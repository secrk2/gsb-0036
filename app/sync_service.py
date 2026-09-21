"""织云系统 - 上游数据同步领域服务。

三类实体（应用 / 模块 / 环境）从模拟上游周期性推入，每趟对账遵循以下规则：

1. 结果分桶：created 新增 / updated 改动 / unchanged 无变化 / conflict 两边都改 /
   failed 校验未通过 / local_deleted 本地已删（墓碑挡住复活）/ upstream_deleted 上游已删本地待处理。
2. 冲突用三方对比（common 上次同步基线、local 本地现值、upstream 这趟上游值）：
   - 只有上游动过 → 直接更新；只有本地动过 → 保留本地；两边都动且取值不同 → 挂起，
     把两边差异摆出来由人裁决，绝不让后到的悄悄盖掉先到的。
3. 裁决后基线前移到"已看过的上游值"：留本地时本地相对新基线仍是一次刻意的本地偏离，
   上游再推同一个值会被识别为"本地改过、上游没动"而保留；上游再改则重新挂起冲突。
4. 本地删除写墓碑（sync_tombstones）：同标识再被推来只记拒收，不复活成新记录。
5. 上游删除（报文里消失）登记 sync_remote_deletions：本地不能假装没看见，
   由人选择"下线本地实体"或"确认忽略"，处理动作与处理人留痕。
"""
import json
import re
import threading
import time

from . import sync_source
from .db import (
    CLUSTERS, ENVIRONMENTS, ENV_LABELS, ENV_KEY_PATTERN, STATUS_LABELS, STATUS_ORDER,
    STATUSES, get_conn, query, query_one,
)

RUNNING_LOCK = threading.Lock()

ENTITY_LABELS = {"app": "应用", "module": "模块", "environment": "环境"}
RESULT_LABELS = {
    "created": "新增",
    "updated": "改动",
    "unchanged": "无变化",
    "conflict": "两边改动待裁决",
    "failed": "未通过",
    "local_deleted": "本地已删·拒收复活",
    "upstream_deleted": "上游已删·待处理",
}
MODULE_TYPES = ["service", "job", "middleware", "frontend"]
MODULE_TYPE_LABELS = {"service": "服务", "job": "定时任务", "middleware": "中间件", "frontend": "前端"}

APP_FIELD_LABELS = {
    "name": "应用名",
    "business_line": "业务线",
    "cluster": "集群",
    "environment": "所属环境",
    "status": "生命周期状态",
    "description": "描述",
}
MOD_FIELD_LABELS = {"name": "模块名", "module_type": "模块类型", "description": "描述"}
ENV_FIELD_LABELS = {"env_label": "环境名称"}

# 同步落库/自动更新允许的应用字段（业务线、环境、状态等都跟随上游）
_APP_DEFAULTS = {"cluster": "华东1集群", "environment": "dev",
                 "status": "developing", "description": ""}


class SyncRunningError(Exception):
    """已有一趟同步在跑（调度与手动撞车），拒绝重复触发。"""


# ---------------------------------------------------------------- 小工具

def _now() -> int:
    return int(time.time())


def _json_loads(raw: str, default=None):
    try:
        return json.loads(raw or "")
    except (ValueError, TypeError):
        return {} if default is None else default


def settings_row() -> dict:
    row = query_one("SELECT * FROM sync_settings WHERE id=1")
    if not row:
        now = _now()
        get_conn().execute(
            "INSERT INTO sync_settings (id, enabled, interval_seconds, source_scene, updated_at) "
            "VALUES (1,0,300,'steady',?)", (now,))
        get_conn().commit()
        row = query_one("SELECT * FROM sync_settings WHERE id=1")
    return dict(row)


def update_settings(enabled: bool, interval_seconds: int, scene: str, user: dict) -> dict:
    if scene not in sync_source.SCENES:
        raise ValueError(f"非法模拟源剧本：{scene}")
    if not (30 <= int(interval_seconds) <= 86400):
        raise ValueError("同步间隔需在 30 秒 ~ 24 小时（86400 秒）之间")
    old = settings_row()
    now = _now()
    get_conn().execute(
        "UPDATE sync_settings SET enabled=?, interval_seconds=?, source_scene=?, updated_by=?, updated_at=? WHERE id=1",
        (1 if enabled else 0, int(interval_seconds), scene, user["id"], now),
    )
    get_conn().commit()
    changes = []
    if bool(old["enabled"]) != enabled:
        changes.append(f"定时同步{'开启' if enabled else '关闭'}")
    if old["interval_seconds"] != int(interval_seconds):
        changes.append(f"间隔 {old['interval_seconds']}s → {int(interval_seconds)}s")
    if old["source_scene"] != scene:
        changes.append(f"模拟源剧本 → {sync_source.SCENE_LABELS[scene]}")
    if changes:
        log_audit(user["id"], "setting", "", "", "；".join(changes), None)
    return settings_row()


def log_audit(actor_id, action: str, entity_type: str, external_key: str,
              detail: str, run_id: int | None) -> None:
    get_conn().execute(
        """INSERT INTO sync_audit_logs
           (actor_id, action, entity_type, external_key, detail, run_id, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (actor_id, action, entity_type, external_key, detail, run_id, _now()),
    )
    get_conn().commit()


# ---------------------------------------------------------------- 归一化与校验

def _bl_code_to_id(code: str):
    row = query_one("SELECT id, name FROM business_lines WHERE code=?", (str(code or "").strip(),))
    return (row["id"], row["name"]) if row else (None, None)


def normalize_app(payload: dict) -> dict:
    return {
        "name": str(payload.get("name", "")).strip(),
        "business_line": str(payload.get("business_line", "")).strip(),
        "cluster": str(payload.get("cluster") or _APP_DEFAULTS["cluster"]).strip(),
        "environment": str(payload.get("environment") or _APP_DEFAULTS["environment"]).strip(),
        "status": str(payload.get("status") or _APP_DEFAULTS["status"]).strip(),
        "description": str(payload.get("description") or "").strip(),
    }


def validate_app(fields: dict) -> str | None:
    if not fields["name"]:
        return "应用名为空"
    bl_id, _ = _bl_code_to_id(fields["business_line"])
    if bl_id is None:
        return f"业务线编码不存在：{fields['business_line']}"
    if fields["cluster"] not in CLUSTERS:
        return f"集群不在本地登记范围：{fields['cluster']}（允许：{'、'.join(CLUSTERS)}）"
    if fields["environment"] not in ENVIRONMENTS:
        return f"所属环境非法：{fields['environment']}（允许：{'/'.join(ENVIRONMENTS)}）"
    if fields["status"] not in STATUSES:
        return f"生命周期状态非法：{fields['status']}"
    return None


def normalize_module(payload: dict) -> dict:
    return {
        "name": str(payload.get("name", "")).strip(),
        "module_type": str(payload.get("module_type") or "service").strip(),
        "description": str(payload.get("description") or "").strip(),
    }


def validate_module(fields: dict) -> str | None:
    if not fields["name"]:
        return "模块名为空"
    if fields["module_type"] not in MODULE_TYPES:
        return f"模块类型非法：{fields['module_type']}"
    return None


def normalize_env(payload: dict) -> dict:
    return {
        "env_key": str(payload.get("env_key", "")).strip().lower(),
        "env_label": str(payload.get("env_label", "")).strip(),
    }


def validate_env(fields: dict, app_id: int) -> str | None:
    if not re.fullmatch(ENV_KEY_PATTERN, fields["env_key"] or ""):
        return f"环境标识非法：{fields['env_key']}（小写字母开头，仅含小写字母数字_-.）"
    if not fields["env_label"]:
        return "环境名称为空"
    if query_one("SELECT 1 FROM app_environments WHERE app_id=? AND env_key=?",
                 (app_id, fields["env_key"])):
        # 同应用下另一条非本同步来源的环境已占用该键
        row = query_one("SELECT external_id FROM app_environments WHERE app_id=? AND env_key=?",
                        (app_id, fields["env_key"]))
        if not row["external_id"]:
            return f"该应用下已存在本地环境「{fields['env_key']}」，同步键冲突"
    return None


def local_app_snapshot(row) -> dict:
    bl = query_one("SELECT code FROM business_lines WHERE id=?", (row["business_line_id"],))
    return {
        "name": row["name"],
        "business_line": bl["code"] if bl else "",
        "cluster": row["cluster"],
        "environment": row["environment"],
        "status": row["status"],
        "description": row["description"] or "",
    }


def local_module_snapshot(row) -> dict:
    return {"name": row["name"], "module_type": row["module_type"],
            "description": row["description"] or ""}


def local_env_snapshot(row) -> dict:
    return {"env_label": row["env_label"]}


# ---------------------------------------------------------------- 字段级三方对比

def _field_labels(entity_type: str) -> dict:
    return {"app": APP_FIELD_LABELS, "module": MOD_FIELD_LABELS,
            "environment": ENV_FIELD_LABELS}[entity_type]


def _display_value(entity_type: str, field: str, value, scope_row=None) -> str:
    if entity_type == "app":
        if field == "business_line":
            _, name = _bl_code_to_id(value)
            return name or value
        if field == "environment":
            return ENV_LABELS.get(value, value)
        if field == "status":
            return STATUS_LABELS.get(value, value)
    if entity_type == "module" and field == "module_type":
        return MODULE_TYPE_LABELS.get(value, value)
    return "" if value is None else str(value)


def three_way(entity_type: str, common: dict, local: dict, upstream: dict) -> list[dict]:
    """逐字段三方对比，返回每个字段一条 {field, label, common, local, upstream, state}。

    state: same 三方一致 / upstream_only 仅上游改（可自动收）/ local_only 仅本地改（保留本地）/
           both_same 两边都改成了同值 / conflict 两边改成不同值（必须裁决）。
    """
    labels = _field_labels(entity_type)
    entries = []
    for field, label in labels.items():
        c, l, u = common.get(field, ""), local.get(field, ""), upstream.get(field, "")
        if l == u:
            state = "same" if c == l else "both_same"
        elif l == c and u != c:
            state = "upstream_only"
        elif u == c and l != c:
            state = "local_only"
        else:
            state = "conflict"
        entries.append({
            "field": field, "label": label,
            "common": _display_value(entity_type, field, c),
            "local": _display_value(entity_type, field, l),
            "upstream": _display_value(entity_type, field, u),
            "raw_common": c, "raw_local": l, "raw_upstream": u,
            "state": state,
        })
    return entries


def classify(entries: list[dict]) -> str:
    """根据逐字段对比给整条记录定结果：updated / unchanged / conflict。"""
    if any(e["state"] == "conflict" for e in entries):
        return "conflict"
    if any(e["state"] == "upstream_only" for e in entries):
        return "updated"
    return "unchanged"


# ---------------------------------------------------------------- 同步主流程

def _insert_item(conn, run_id: int, entity_type: str, key: str, name: str,
                 result: str, reason: str, detail: dict | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO sync_items
           (run_id, entity_type, external_key, name, result, reason, detail_json)
           VALUES (?,?,?,?,?,?,?)""",
        (run_id, entity_type, key, name, result, reason,
         json.dumps(detail or {}, ensure_ascii=False)),
    )
    return cur.lastrowid


def run_sync(trigger_type: str, user_id: int | None, scene_override: str | None = None) -> dict:
    """跑一趟同步。manual 必须带触发人；scheduled 触发人为 NULL。"""
    if not RUNNING_LOCK.acquire(blocking=False):
        raise SyncRunningError("上一趟同步仍在执行中，请稍后再试")
    start = time.monotonic()
    started_at = _now()
    try:
        s = settings_row()
        scene = scene_override or s["source_scene"]
        if scene_override and scene_override not in sync_source.SCENES:
            raise ValueError(f"非法模拟源剧本：{scene_override}")
        conn = get_conn()
        cur = conn.execute(
            """INSERT INTO sync_runs
               (trigger_type, triggered_by, started_at, status, source_scene, created_at)
               VALUES (?,?,?, 'running', ?, ?)""",
            (trigger_type, user_id, started_at, scene, started_at),
        )
        run_id = cur.lastrowid
        conn.commit()
        actor = user_id or "系统调度"
        log_audit(user_id, "trigger", "", "",
                  f"{'手动' if trigger_type == 'manual' else '定时'}触发一趟同步（模拟源："
                  f"{sync_source.SCENE_LABELS.get(scene, scene)}）", run_id)

        try:
            batch = sync_source.fetch_batch(scene)
        except Exception as e:  # 上游源本身不可用：整趟失败
            return _finish_failed(run_id, started_at, start, f"拉取上游报文失败：{e}", user_id)

        counters = {k: 0 for k in (
            "created_count", "updated_count", "unchanged_count", "conflict_count",
            "failed_count", "local_deleted_count", "upstream_deleted_count")}
        try:
            # 先应用，后对账删除：应用必须先于其模块/环境落库
            apps = [b for b in batch if b["entity_type"] == "app"]
            others = [b for b in batch if b["entity_type"] != "app"]
            app_id_by_key: dict[str, int] = {}
            for item in apps:
                aid = _process_app(conn, run_id, item, counters)
                if aid:
                    app_id_by_key[item["external_key"]] = aid
            conn.commit()
            # 第二遍：模块/环境的父应用可能就是本趟新建的
            for item in others:
                _process_child(conn, run_id, item, counters, app_id_by_key)
            conn.commit()
            # 上游删除对账（报文里消失、本地仍挂着的同步来源实体）
            _detect_remote_deletions(conn, run_id, batch, counters)
            conn.commit()

            total = len(batch)
            failed_reasons = [
                dict(r) for r in query(
                    "SELECT entity_type, external_key, name, reason FROM sync_items "
                    "WHERE run_id=? AND result='failed'", (run_id,))
            ]
            duration_ms = int((time.monotonic() - start) * 1000)
            if counters["failed_count"] or counters["conflict_count"]:
                status = "partial"
            else:
                status = "success"
            conn.execute(
                """UPDATE sync_runs SET finished_at=?, duration_ms=?, status=?,
                   total_count=?, created_count=?, updated_count=?, unchanged_count=?,
                   conflict_count=?, failed_count=?, local_deleted_count=?,
                   upstream_deleted_count=? WHERE id=?""",
                (_now(), duration_ms, status, total,
                 counters["created_count"], counters["updated_count"], counters["unchanged_count"],
                 counters["conflict_count"], counters["failed_count"],
                 counters["local_deleted_count"], counters["upstream_deleted_count"], run_id),
            )
            conn.commit()
            detail = (
                f"同步完成：共 {total} 条，新增 {counters['created_count']}，"
                f"改动 {counters['updated_count']}，无变化 {counters['unchanged_count']}，"
                f"两边改动待裁决 {counters['conflict_count']}，未通过 {counters['failed_count']}，"
                f"拒收复活 {counters['local_deleted_count']}，上游已删待处理 "
                f"{counters['upstream_deleted_count']}；耗时 {duration_ms}ms"
                + (f"；失败原因：{'; '.join(r['reason'] for r in failed_reasons)}"
                   if failed_reasons else "")
            )
            log_audit(None if trigger_type == "scheduled" else user_id,
                      "run_finish", "", "", detail, run_id)
            return {"run_id": run_id, "status": status, "total": total, **counters,
                    "duration_ms": duration_ms, "failed_reasons": failed_reasons}
        except Exception as e:
            conn.rollback()
            return _finish_failed(run_id, started_at, start, f"同步处理中断：{e}", user_id)
    finally:
        RUNNING_LOCK.release()


def _finish_failed(run_id, started_at, start_monotonic, message, actor_id=None) -> dict:
    now = _now()
    conn = get_conn()
    conn.execute(
        "UPDATE sync_runs SET finished_at=?, duration_ms=?, status='failed', error_message=? WHERE id=?",
        (now, int((time.monotonic() - start_monotonic) * 1000), message, run_id),
    )
    conn.commit()
    log_audit(actor_id, "run_finish", "", "", f"同步失败：{message}", run_id)
    return {"run_id": run_id, "status": "failed", "error": message}


def _is_tombstone(entity_type: str, key: str):
    return query_one(
        "SELECT * FROM sync_tombstones WHERE entity_type=? AND external_key=?",
        (entity_type, key),
    )


# ---------------------------------------------------------------- 应用

def _process_app(conn, run_id: int, item: dict, counters: dict) -> int | None:
    key, payload = item["external_key"], item["payload"]
    fields = normalize_app(payload)
    name = fields["name"] or key

    tomb = _is_tombstone("app", key)
    if tomb:
        conn.execute(
            "UPDATE sync_tombstones SET last_pushed_run=? WHERE entity_type=? AND external_key=?",
            (run_id, "app", key))
        _insert_item(conn, run_id, "app", key, tomb["name"], "local_deleted",
                     f"本地已于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(tomb['deleted_at']))} "
                     f"删除该应用，已拒收上游推送，不会复活为新记录")
        counters["local_deleted_count"] += 1
        return None

    bad = validate_app(fields)
    if bad:
        _insert_item(conn, run_id, "app", key, name, "failed", bad, {"upstream": fields})
        counters["failed_count"] += 1
        return None

    row = query_one("SELECT * FROM applications WHERE external_id=?", (key,))
    if row is None:
        # 同业务线下重名（本地手工建过同名应用但没有上游标识）受唯一约束保护：
        # 按条失败并说清原因，不能让整趟同步在数据库层中断。
        bl_id, _ = _bl_code_to_id(fields["business_line"])
        dup = query_one("SELECT id FROM applications WHERE business_line_id=? AND name=?",
                        (bl_id, fields["name"]))
        if dup:
            _insert_item(conn, run_id, "app", key, name, "failed",
                         f"本地已存在同名应用「{fields['name']}」（#{dup['id']}，非上游来源），"
                         f"同步不会另建一条造成重名", {"upstream": fields})
            counters["failed_count"] += 1
            return None
        return _create_app(conn, run_id, key, fields, counters)

    # 生命周期只能向前：上游推来回退状态（如本地已下线、上游却推在研）按条拦下
    if STATUS_ORDER.get(fields["status"], 0) < STATUS_ORDER.get(row["status"], 0):
        bad = (f"上游状态「{STATUS_LABELS.get(fields['status'], fields['status'])}」"
               f"早于本地当前状态「{STATUS_LABELS.get(row['status'], row['status'])}」："
               f"生命周期只能向前流转，拒绝回退；如确认上游口径，请先在本地人工处理")
        _insert_item(conn, run_id, "app", key, name, "failed", bad, {"upstream": fields})
        counters["failed_count"] += 1
        return row["id"]

    common = _json_loads(row["baseline_payload"], default={})
    local = local_app_snapshot(row)
    entries = three_way("app", common, local, fields)
    result = classify(entries)
    if result == "updated":
        _apply_app_update(conn, row["id"], fields)
        changed = [e for e in entries if e["state"] == "upstream_only"]
        reason = "上游改动已同步：" + "；".join(
            f"{e['label']} {e['common'] or '（空）'} → {e['upstream'] or '（空）'}" for e in changed)
        _insert_item(conn, run_id, "app", key, fields["name"], "updated", reason,
                     {"entries": entries})
        conn.execute(
            "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?, '同步更新', ?, ?)",
            (row["id"], None, reason, _now()))
        counters["updated_count"] += 1
        return row["id"]
    if result == "conflict":
        _enqueue_conflict(conn, run_id, "app", key, row["id"], fields["name"],
                          common, local, fields, entries)
        _insert_item(conn, run_id, "app", key, fields["name"], "conflict",
                     "本地与上游都改动了该应用，已挂起等待人工裁决（不会用任一侧静默覆盖另一侧）",
                     {"conflict_fields": [e["label"] for e in entries if e["state"] == "conflict"],
                      "entries": entries})
        counters["conflict_count"] += 1
        return row["id"]
    # unchanged：区分"本地改过但上游没动"与"两边改成同值"，让人看得懂为什么没收上游值
    local_only = [e["label"] for e in entries if e["state"] == "local_only"]
    both_same = [e["label"] for e in entries if e["state"] == "both_same"]
    reason = ""
    if both_same:
        reason = "本地与上游各自改动后取值一致：" + "、".join(both_same)
    elif local_only:
        reason = "本地有改动而上游未变化，保留本地值：" + "、".join(local_only)
    _insert_item(conn, run_id, "app", key, fields["name"], "unchanged", reason,
                 {"entries": entries} if reason else None)
    counters["unchanged_count"] += 1
    return row["id"]


def _create_app(conn, run_id: int, key: str, fields: dict, counters: dict) -> int:
    from . import env_service as esvc
    bl_id, _ = _bl_code_to_id(fields["business_line"])
    now = _now()
    cur = conn.execute(
        """INSERT INTO applications
           (name, business_line_id, owner_id, cluster, environment, status,
            description, created_at, updated_at, external_id, baseline_payload)
           VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (fields["name"], bl_id, fields["cluster"], fields["environment"], fields["status"],
         fields["description"], now, now, key, json.dumps(fields, ensure_ascii=False)),
    )
    app_id = cur.lastrowid
    conn.execute(
        "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) "
        "VALUES (?,?, '同步新增', ?, ?)",
        (app_id, None, f"应用由上游同步新增（标识 {key}）", now))
    # 同步新建的应用同样开通四个标准环境
    esvc.ensure_default_environments(app_id, now)
    _insert_item(conn, run_id, "app", key, fields["name"], "created",
                 f"上游新应用已登记：{fields['name']}（{fields['business_line']}，"
                 f"{STATUS_LABELS[fields['status']]}）")
    counters["created_count"] += 1
    return app_id


def _apply_app_update(conn, app_id: int, fields: dict) -> None:
    bl_id, _ = _bl_code_to_id(fields["business_line"])
    # 状态回退保护：即使上游报文带较早状态，本地生命周期也不向后退（取更靠后的状态）
    cur = conn.execute("SELECT status FROM applications WHERE id=?", (app_id,)).fetchone()
    new_status = fields["status"]
    if cur and STATUS_ORDER.get(new_status, 0) < STATUS_ORDER.get(cur["status"], 0):
        new_status = cur["status"]
    conn.execute(
        """UPDATE applications SET name=?, business_line_id=?, cluster=?, environment=?,
           status=?, description=?, updated_at=?, baseline_payload=? WHERE id=?""",
        (fields["name"], bl_id, fields["cluster"], fields["environment"], new_status,
         fields["description"], _now(), json.dumps(fields, ensure_ascii=False), app_id),
    )


# ---------------------------------------------------------------- 模块 / 环境

def _resolve_parent(conn, item: dict, app_id_by_key: dict):
    parent_key = item.get("parent_app_key")
    if parent_key in app_id_by_key:
        row = query_one("SELECT id, name FROM applications WHERE id=?",
                        (app_id_by_key[parent_key],))
        return row
    return query_one("SELECT id, name FROM applications WHERE external_id=?", (parent_key,))


def _process_child(conn, run_id: int, item: dict, counters: dict, app_id_by_key: dict) -> None:
    etype, key = item["entity_type"], item["external_key"]
    if etype == "module":
        fields, name = normalize_module(item["payload"]), None
        name = fields["name"] or key
    else:
        fields = normalize_env(item["payload"])
        name = fields["env_label"] or key

    tomb = _is_tombstone(etype, key)
    if tomb:
        conn.execute(
            "UPDATE sync_tombstones SET last_pushed_run=? WHERE entity_type=? AND external_key=?",
            (run_id, etype, key))
        _insert_item(conn, run_id, etype, key, tomb["name"], "local_deleted",
                     f"本地已于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(tomb['deleted_at']))} "
                     f"删除该{ENTITY_LABELS[etype]}，已拒收上游推送，不会复活")
        counters["local_deleted_count"] += 1
        return

    parent = _resolve_parent(conn, item, app_id_by_key)
    if parent is None:
        _insert_item(conn, run_id, etype, key, name, "failed",
                     f"所属应用（上游标识 {item.get('parent_app_key')}）在上游报文与本地均不存在，"
                     f"无法挂接该{ENTITY_LABELS[etype]}",
                     {"upstream": fields})
        counters["failed_count"] += 1
        return

    if etype == "module":
        bad = validate_module(fields)
        if bad:
            _insert_item(conn, run_id, etype, key, name, "failed", bad, {"upstream": fields})
            counters["failed_count"] += 1
            return
        _process_module(conn, run_id, key, parent, fields, counters)
    else:
        bad = validate_env(fields, parent["id"])
        if bad:
            _insert_item(conn, run_id, etype, key, name, "failed", bad, {"upstream": fields})
            counters["failed_count"] += 1
            return
        _process_environment(conn, run_id, key, parent, fields, counters)


def _process_module(conn, run_id, key, parent, fields, counters) -> None:
    row = query_one("SELECT * FROM sync_modules WHERE external_id=?", (key,))
    if row is None:
        now = _now()
        conn.execute(
            """INSERT INTO sync_modules
               (app_id, external_id, name, module_type, description, status,
                baseline_payload, created_at, updated_at)
               VALUES (?,?,?,?,?, 'active', ?, ?, ?)""",
            (parent["id"], key, fields["name"], fields["module_type"], fields["description"],
             json.dumps(fields, ensure_ascii=False), now, now),
        )
        _insert_item(conn, run_id, "module", key, fields["name"], "created",
                     f"上游新模块已挂到应用「{parent['name']}」下")
        counters["created_count"] += 1
        return

    common = _json_loads(row["baseline_payload"], default={})
    local = local_module_snapshot(row)
    entries = three_way("module", common, local, fields)
    result = classify(entries)
    if result == "updated":
        conn.execute(
            """UPDATE sync_modules SET name=?, module_type=?, description=?, updated_at=?,
               baseline_payload=? WHERE id=?""",
            (fields["name"], fields["module_type"], fields["description"], _now(),
             json.dumps(fields, ensure_ascii=False), row["id"]),
        )
        changed = [e for e in entries if e["state"] == "upstream_only"]
        reason = "上游改动已同步：" + "；".join(
            f"{e['label']} {e['common'] or '（空）'} → {e['upstream'] or '（空）'}" for e in changed)
        _insert_item(conn, run_id, "module", key, fields["name"], "updated", reason,
                     {"entries": entries})
        counters["updated_count"] += 1
    elif result == "conflict":
        _enqueue_conflict(conn, run_id, "module", key, row["id"], fields["name"],
                          common, local, fields, entries)
        _insert_item(conn, run_id, "module", key, fields["name"], "conflict",
                     "本地与上游都改动了该模块，已挂起等待人工裁决",
                     {"conflict_fields": [e["label"] for e in entries if e["state"] == "conflict"],
                      "entries": entries})
        counters["conflict_count"] += 1
    else:
        local_only = [e["label"] for e in entries if e["state"] == "local_only"]
        both_same = [e["label"] for e in entries if e["state"] == "both_same"]
        reason = ""
        if both_same:
            reason = "本地与上游各自改动后取值一致：" + "、".join(both_same)
        elif local_only:
            reason = "本地有改动而上游未变化，保留本地值：" + "、".join(local_only)
        _insert_item(conn, run_id, "module", key, fields["name"], "unchanged", reason,
                     {"entries": entries} if reason else None)
        counters["unchanged_count"] += 1


def _process_environment(conn, run_id, key, parent, fields, counters) -> None:
    row = query_one("SELECT * FROM app_environments WHERE external_id=?", (key,))
    # 也可能上游环境键与本地自建环境撞键（validate_env 已拦这种情况为 failed）
    if row is None:
        now = _now()
        cur = conn.execute(
            """INSERT INTO app_environments
               (app_id, env_key, env_label, is_builtin, deploy_restricted,
                window_days, window_start, window_end, created_by, created_at, updated_at,
                external_id, baseline_payload)
               VALUES (?,?,?,0,0,'[]','00:00','23:59',NULL,?,?,?,?)""",
            (parent["id"], fields["env_key"], fields["env_label"], now, now,
             key, json.dumps(fields, ensure_ascii=False)),
        )
        _insert_item(conn, run_id, "environment", key, fields["env_label"], "created",
                     f"上游新环境「{fields['env_label']}」已挂到应用「{parent['name']}」下",
                     {"env_id": cur.lastrowid})
        counters["created_count"] += 1
        return

    common = _json_loads(row["baseline_payload"], default={})
    local = local_env_snapshot(row)
    entries = three_way("environment", common, local, fields)
    result = classify(entries)
    if result == "updated":
        conn.execute(
            "UPDATE app_environments SET env_label=?, updated_at=?, baseline_payload=? WHERE id=?",
            (fields["env_label"], _now(), json.dumps(fields, ensure_ascii=False), row["id"]),
        )
        changed = [e for e in entries if e["state"] == "upstream_only"]
        reason = "上游改动已同步：" + "；".join(
            f"{e['label']} {e['common'] or '（空）'} → {e['upstream'] or '（空）'}" for e in changed)
        _insert_item(conn, run_id, "environment", key, fields["env_label"], "updated", reason,
                     {"entries": entries})
        counters["updated_count"] += 1
    elif result == "conflict":
        _enqueue_conflict(conn, run_id, "environment", key, row["id"], fields["env_label"],
                          common, local, fields, entries)
        _insert_item(conn, run_id, "environment", key, fields["env_label"], "conflict",
                     "本地与上游都改动了该环境，已挂起等待人工裁决",
                     {"conflict_fields": [e["label"] for e in entries if e["state"] == "conflict"],
                      "entries": entries})
        counters["conflict_count"] += 1
    else:
        local_only = [e["label"] for e in entries if e["state"] == "local_only"]
        reason = ("本地有改动而上游未变化，保留本地值：" + "、".join(local_only)) if local_only else ""
        _insert_item(conn, run_id, "environment", key, fields["env_label"], "unchanged", reason,
                     {"entries": entries} if reason else None)
        counters["unchanged_count"] += 1


# ---------------------------------------------------------------- 冲突入队

def _enqueue_conflict(conn, run_id, etype, key, local_id, name,
                      common, local, upstream, entries) -> None:
    conflict_fields = [e["field"] for e in entries if e["state"] == "conflict"]
    # 部分唯一索引保证同一实体同时只有一条 pending；重复挂起时刷新载荷为最新一趟
    existing = conn.execute(
        "SELECT id FROM sync_conflicts WHERE entity_type=? AND external_key=? AND status='pending'",
        (etype, key),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE sync_conflicts SET run_id=?, name=?, common_payload=?, local_payload=?,
               upstream_payload=?, changed_fields=? WHERE id=?""",
            (run_id, name, json.dumps(common, ensure_ascii=False),
             json.dumps(local, ensure_ascii=False), json.dumps(upstream, ensure_ascii=False),
             json.dumps(conflict_fields, ensure_ascii=False), existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO sync_conflicts
               (run_id, entity_type, external_key, local_entity_id, name,
                common_payload, local_payload, upstream_payload, changed_fields, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run_id, etype, key, local_id, name,
             json.dumps(common, ensure_ascii=False), json.dumps(local, ensure_ascii=False),
             json.dumps(upstream, ensure_ascii=False),
             json.dumps(conflict_fields, ensure_ascii=False), _now()),
        )


# ---------------------------------------------------------------- 上游删除对账

def _detect_remote_deletions(conn, run_id, batch, counters) -> None:
    pushed = {"app": set(), "module": set(), "environment": set()}
    for b in batch:
        pushed[b["entity_type"]].add(b["external_key"])

    # 已经登记过（含已处理）的不再重复登记；仍 pending 的每趟重新冒到报告里
    known = {
        (r["entity_type"], r["external_key"]): dict(r)
        for r in query("SELECT * FROM sync_remote_deletions")
    }

    def register(etype, key, name, local_id):
        now = _now()
        conn.execute(
            """INSERT INTO sync_remote_deletions
               (entity_type, external_key, name, local_entity_id,
                first_seen_run, first_seen_at, status)
               VALUES (?,?,?,?,?,?,'pending')""",
            (etype, key, name, local_id, run_id, now),
        )
        _insert_item(conn, run_id, etype, key, name, "upstream_deleted",
                     "上游报文里已没有这条记录（上游已删除），本地仍挂着，已列入待处理："
                     "可选择下线本地实体或确认忽略，不会假装没看见")
        counters["upstream_deleted_count"] += 1

    def resurface(rec):
        _insert_item(conn, run_id, rec["entity_type"], rec["external_key"], rec["name"],
                     "upstream_deleted",
                     f"上游已于更早趟次（运行 #{rec['first_seen_run']}）删除该记录，"
                     f"本地仍挂着，等待处理（下线本地实体 / 确认忽略）")
        counters["upstream_deleted_count"] += 1

    # 应用：external_id 非空且不在报文里（墓碑对应的本地行已不存在，自然查不到）
    for r in conn.execute(
        "SELECT id, name, external_id FROM applications "
        "WHERE external_id IS NOT NULL AND external_id != ''"
    ).fetchall():
        key = r["external_id"]
        if key in pushed["app"]:
            continue
        rec = known.get(("app", key))
        if rec is None:
            register("app", key, r["name"], r["id"])
        elif rec["status"] == "pending":
            resurface(rec)

    # 模块
    for r in conn.execute(
        "SELECT id, name, external_id, status FROM sync_modules "
        "WHERE external_id IS NOT NULL AND external_id != '' AND status='active'"
    ).fetchall():
        key = r["external_id"]
        if key in pushed["module"]:
            continue
        rec = known.get(("module", key))
        if rec is None:
            register("module", key, r["name"], r["id"])
        elif rec["status"] == "pending":
            resurface(rec)

    # 环境
    for r in conn.execute(
        "SELECT id, env_label, external_id FROM app_environments "
        "WHERE external_id IS NOT NULL AND external_id != ''"
    ).fetchall():
        key = r["external_id"]
        if key in pushed["environment"]:
            continue
        rec = known.get(("environment", key))
        if rec is None:
            register("environment", key, r["env_label"], r["id"])
        elif rec["status"] == "pending":
            resurface(rec)


# ---------------------------------------------------------------- 冲突裁决

def get_conflict(conflict_id: int) -> dict | None:
    row = query_one("SELECT * FROM sync_conflicts WHERE id=?", (conflict_id,))
    return dict(row) if row else None


def conflict_detail(row: dict) -> dict:
    common = _json_loads(row["common_payload"], default={})
    local = _json_loads(row["local_payload"], default={})
    upstream = _json_loads(row["upstream_payload"], default={})
    entries = three_way(row["entity_type"], common, local, upstream)
    return {
        "id": row["id"], "run_id": row["run_id"],
        "entity_type": row["entity_type"],
        "entity_label": ENTITY_LABELS[row["entity_type"]],
        "external_key": row["external_key"], "local_entity_id": row["local_entity_id"],
        "name": row["name"], "status": row["status"],
        "changed_fields": _json_loads(row["changed_fields"], default=[]),
        "entries": entries,
        "decided_by_name": None, "decided_at": row["decided_at"], "decide_note": row["decide_note"],
        "created_at": row["created_at"],
    }


def decide_conflict(conflict_id: int, choice: str, note: str, user: dict) -> dict:
    if choice not in ("keep_local", "take_upstream"):
        raise ValueError("裁决动作必须是 keep_local（留本地）或 take_upstream（取上游）")
    row = query_one("SELECT * FROM sync_conflicts WHERE id=?", (conflict_id,))
    if row is None:
        raise LookupError("冲突记录不存在")
    if row["status"] != "pending":
        raise ValueError(f"该冲突已被裁决（{row['status']}），不能重复决定")
    note = (note or "").strip()
    upstream = _json_loads(row["upstream_payload"], default={})
    common = _json_loads(row["common_payload"], default={})
    local = _json_loads(row["local_payload"], default={})
    etype, key = row["entity_type"], row["external_key"]
    now = _now()
    conn = get_conn()

    if choice == "take_upstream":
        _apply_decision(conn, etype, row["local_entity_id"], key, upstream)
        chosen_text = "采用上游值"
    else:
        chosen_text = "保留本地值"

    # 无论留哪边，基线都前移到"已看过的上游值"：
    # 取上游 → 本地=基线；留本地 → 本地相对新基线是一次刻意偏离，上游原样再推不会覆盖。
    _update_baseline(conn, etype, row["local_entity_id"], key, upstream)

    conn.execute(
        "UPDATE sync_conflicts SET status=?, decided_by=?, decided_at=?, decide_note=? WHERE id=?",
        (choice, user["id"], now, note, conflict_id),
    )
    diff_fields = [e["label"] for e in three_way(etype, common, local, upstream)
                   if e["state"] == "conflict"]
    detail = (f"冲突裁决：{ENTITY_LABELS[etype]}「{row['name']}」（{key}）→ {chosen_text}；"
              f"撞车字段：{'、'.join(diff_fields) or '（无）'}"
              + (f"；裁决备注：{note}" if note else ""))
    conn.execute(
        """INSERT INTO sync_audit_logs
           (actor_id, action, entity_type, external_key, detail, run_id, created_at)
           VALUES (?, 'conflict', ?, ?, ?, ?, ?)""",
        (user["id"], etype, key, detail, row["run_id"], now),
    )
    conn.commit()
    return {"ok": True, "choice": choice, "detail": detail}


def _apply_decision(conn, etype, local_id, key, upstream) -> None:
    if etype == "app":
        row = conn.execute("SELECT * FROM applications WHERE external_id=?", (key,)).fetchone()
        if row:
            # 复用同步更新路径：含生命周期回退保护，随后基线由 _update_baseline 统一前移
            _apply_app_update(conn, row["id"], upstream)
            conn.execute(
                "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?, '冲突裁决', ?, ?)",
                (row["id"], None, f"冲突裁决采用上游值（标识 {key}）", _now()))
    elif etype == "module":
        row = conn.execute("SELECT * FROM sync_modules WHERE external_id=?", (key,)).fetchone()
        if row:
            conn.execute(
                "UPDATE sync_modules SET name=?, module_type=?, description=?, updated_at=? WHERE id=?",
                (upstream.get("name", row["name"]), upstream.get("module_type", row["module_type"]),
                 upstream.get("description", ""), _now(), row["id"]),
            )
    else:
        row = conn.execute("SELECT * FROM app_environments WHERE external_id=?", (key,)).fetchone()
        if row:
            conn.execute(
                "UPDATE app_environments SET env_label=?, updated_at=? WHERE id=?",
                (upstream.get("env_label", row["env_label"]), _now(), row["id"]),
            )


def _update_baseline(conn, etype, local_id, key, upstream) -> None:
    payload = json.dumps(upstream, ensure_ascii=False)
    table = {"app": "applications", "module": "sync_modules",
             "environment": "app_environments"}[etype]
    conn.execute(f"UPDATE {table} SET baseline_payload=? WHERE external_id=?", (payload, key))


# ---------------------------------------------------------------- 上游删除处理

def list_remote_deletions(status: str | None = None) -> list[dict]:
    sql = """SELECT d.*, u.name AS handled_by_name
             FROM sync_remote_deletions d LEFT JOIN users u ON u.id=d.handled_by"""
    params = []
    if status:
        sql += " WHERE d.status=?"
        params.append(status)
    sql += " ORDER BY CASE d.status WHEN 'pending' THEN 0 ELSE 1 END, d.first_seen_at DESC, d.id"
    rows = query(sql, tuple(params))
    result = []
    for r in rows:
        d = dict(r)
        d["entity_label"] = ENTITY_LABELS[d["entity_type"]]
        d["status_label"] = {"pending": "待处理", "offlined": "已下线", "ignored": "已忽略"}[d["status"]]
        result.append(d)
    return result


def handle_remote_deletion(deletion_id: int, action: str, note: str, user: dict) -> dict:
    if action not in ("offline", "ignore"):
        raise ValueError("处理动作必须是 offline（下线本地实体）或 ignore（确认忽略）")
    row = query_one("SELECT * FROM sync_remote_deletions WHERE id=?", (deletion_id,))
    if row is None:
        raise LookupError("待处理记录不存在")
    if row["status"] != "pending":
        raise ValueError("该记录已处理，不能重复操作")
    note = (note or "").strip()
    now = _now()
    conn = get_conn()
    detail_extra = ""
    if action == "offline":
        _offline_remote_entity(conn, row)
        new_status, action_text = "offlined", "已将本地实体下线"
    else:
        new_status, action_text = "ignored", "已确认忽略（本地保留，不再提醒上游已删）"
    conn.execute(
        "UPDATE sync_remote_deletions SET status=?, handled_by=?, handled_at=?, handle_note=? WHERE id=?",
        (new_status, user["id"], now, note, deletion_id),
    )
    detail = (f"上游删除处理：{ENTITY_LABELS[row['entity_type']]}「{row['name']}」"
              f"（{row['external_key']}）→ {action_text}"
              + (f"；备注：{note}" if note else ""))
    conn.execute(
        """INSERT INTO sync_audit_logs
           (actor_id, action, entity_type, external_key, detail, run_id, created_at)
           VALUES (?, 'remote_delete', ?, ?, ?, NULL, ?)""",
        (user["id"], row["entity_type"], row["external_key"], detail, now),
    )
    conn.commit()
    return {"ok": True, "status": new_status, "detail": detail}


def _offline_remote_entity(conn, row) -> None:
    etype = row["entity_type"]
    if etype == "app":
        r = conn.execute("SELECT id, status FROM applications WHERE id=?",
                         (row["local_entity_id"],)).fetchone()
        if r:
            # 生命周期不能回退：已下线的保持下线，其余直接置为终态 offline
            if r["status"] != "offline":
                conn.execute(
                    "UPDATE applications SET status='offline', updated_at=? WHERE id=?",
                    (_now(), r["id"]))
                conn.execute(
                    "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) "
                    "VALUES (?,?, '上游删除下线', ?, ?)",
                    (r["id"], None,
                     f"上游已删除该应用，本地按裁决下线（上游标识 {row['external_key']}）", _now()))
    elif etype == "module":
        r = conn.execute("SELECT id FROM sync_modules WHERE id=?",
                         (row["local_entity_id"],)).fetchone()
        if r:
            conn.execute("UPDATE sync_modules SET status='offlined', updated_at=? WHERE id=?",
                         (_now(), r["id"]))
    else:
        r = conn.execute("SELECT id, app_id FROM app_environments WHERE id=?",
                         (row["local_entity_id"],)).fetchone()
        if r:
            # 环境无"下线"态：有挂载则拒绝（交由调用方转 409），否则删除
            mounts = conn.execute(
                "SELECT COUNT(*) AS c FROM config_items WHERE app_id=? AND environment="
                "(SELECT env_key FROM app_environments WHERE id=?)",
                (r["app_id"], r["id"])).fetchone()["c"]
            inst = conn.execute(
                "SELECT COUNT(*) AS c FROM app_instances WHERE env_id=?", (r["id"],)).fetchone()["c"]
            if mounts or inst:
                raise ValueError(
                    f"该环境上还挂着 {mounts} 个配置项、{inst} 个实例，不能随上游删除直接下线；"
                    "请先清理挂载，或选择「确认忽略」")
            conn.execute("DELETE FROM app_environments WHERE id=?", (r["id"],))


# ---------------------------------------------------------------- 本地删除（写墓碑）

def list_local_synced() -> dict:
    apps = [dict(r) for r in query(
        """SELECT a.id, a.name, a.external_id, b.name AS business_line_name, a.status
           FROM applications a JOIN business_lines b ON b.id=a.business_line_id
           WHERE a.external_id IS NOT NULL AND a.external_id != ''
           ORDER BY a.id""")]
    modules = [dict(r) for r in query(
        """SELECT m.id, m.name, m.external_id, m.status, a.name AS app_name
           FROM sync_modules m JOIN applications a ON a.id=m.app_id
           WHERE m.external_id IS NOT NULL AND m.external_id != ''
           ORDER BY m.id""")]
    envs = [dict(r) for r in query(
        """SELECT e.id, e.env_key, e.env_label, e.external_id, a.name AS app_name
           FROM app_environments e JOIN applications a ON a.id=e.app_id
           WHERE e.external_id IS NOT NULL AND e.external_id != ''
           ORDER BY e.id""")]
    return {"apps": apps, "modules": modules, "environments": envs}


def delete_local_synced(etype: str, local_id: int, user: dict) -> None:
    """本地删除一个同步来源实体并立墓碑：之后上游再推同标识记录只记拒收，不复活。"""
    now = _now()
    conn = get_conn()
    if etype == "app":
        r = conn.execute("SELECT id, name, external_id FROM applications WHERE id=?",
                         (local_id,)).fetchone()
        if not r or not r["external_id"]:
            raise LookupError("同步来源应用不存在")
        conn.execute(
            "INSERT OR IGNORE INTO sync_tombstones (entity_type, external_key, name, deleted_by, deleted_at) "
            "VALUES ('app', ?, ?, ?, ?)", (r["external_id"], r["name"], user["id"], now))
        conn.execute("DELETE FROM applications WHERE id=?", (local_id,))
        what = f"本地删除同步应用「{r['name']}」（{r['external_id']}），已立墓碑，上游再推将拒收复活"
    elif etype == "module":
        r = conn.execute(
            "SELECT m.id, m.name, m.external_id FROM sync_modules m WHERE m.id=?",
            (local_id,)).fetchone()
        if not r or not r["external_id"]:
            raise LookupError("同步来源模块不存在")
        conn.execute(
            "INSERT OR IGNORE INTO sync_tombstones (entity_type, external_key, name, deleted_by, deleted_at) "
            "VALUES ('module', ?, ?, ?, ?)", (r["external_id"], r["name"], user["id"], now))
        conn.execute("DELETE FROM sync_modules WHERE id=?", (local_id,))
        what = f"本地删除同步模块「{r['name']}」（{r['external_id']}），已立墓碑，上游再推将拒收复活"
    elif etype == "environment":
        r = conn.execute("SELECT id, env_key, env_label, external_id, app_id FROM app_environments WHERE id=?",
                         (local_id,)).fetchone()
        if not r or not r["external_id"]:
            raise LookupError("同步来源环境不存在")
        mounts = conn.execute(
            "SELECT COUNT(*) AS c FROM config_items WHERE app_id=? AND environment=?",
            (r["app_id"], r["env_key"])).fetchone()["c"]
        inst = conn.execute("SELECT COUNT(*) AS c FROM app_instances WHERE env_id=?",
                            (r["id"],)).fetchone()["c"]
        if mounts or inst:
            raise ValueError(f"该环境上还挂着 {mounts} 个配置项、{inst} 个实例，请先清理挂载再删除")
        conn.execute(
            "INSERT OR IGNORE INTO sync_tombstones (entity_type, external_key, name, deleted_by, deleted_at) "
            "VALUES ('environment', ?, ?, ?, ?)", (r["external_id"], r["env_label"], user["id"], now))
        conn.execute("DELETE FROM app_environments WHERE id=?", (local_id,))
        what = f"本地删除同步环境「{r['env_label']}」（{r['external_id']}），已立墓碑，上游再推将拒收复活"
    else:
        raise ValueError(f"非法实体类型：{etype}")
    conn.execute(
        """INSERT INTO sync_audit_logs
           (actor_id, action, entity_type, external_key, detail, run_id, created_at)
           VALUES (?, 'local_delete', ?, '', ?, NULL, ?)""",
        (user["id"], etype, what, now))
    conn.commit()


# ---------------------------------------------------------------- 查询序列化

def list_tombstones() -> list[dict]:
    rows = query(
        """SELECT t.*, u.name AS deleted_by_name, r.started_at AS last_pushed_at
           FROM sync_tombstones t
           LEFT JOIN users u ON u.id=t.deleted_by
           LEFT JOIN sync_runs r ON r.id=t.last_pushed_run
           ORDER BY t.deleted_at DESC, t.id""")
    out = []
    for r in rows:
        d = dict(r)
        d["entity_label"] = ENTITY_LABELS[d["entity_type"]]
        d["deleted_by_name"] = d["deleted_by_name"] or "（早期数据）"
        out.append(d)
    return out


def list_runs(limit: int = 30) -> list[dict]:
    rows = query(
        """SELECT r.*, u.name AS triggered_by_name FROM sync_runs r
           LEFT JOIN users u ON u.id=r.triggered_by
           ORDER BY r.id DESC LIMIT ?""", (min(int(limit), 100),))
    return [_run_dict(r) for r in rows]


def _run_dict(r) -> dict:
    d = dict(r)
    d["trigger_label"] = "手动" if d["trigger_type"] == "manual" else "定时"
    d["status_label"] = {"running": "运行中", "success": "成功", "partial": "部分挂起/失败",
                         "failed": "失败"}[d["status"]]
    d["triggered_by_name"] = d.get("triggered_by_name") or "系统调度"
    d["scene_label"] = sync_source.SCENE_LABELS.get(d["source_scene"], d["source_scene"])
    return d


def get_run(run_id: int) -> dict | None:
    row = query_one(
        """SELECT r.*, u.name AS triggered_by_name FROM sync_runs r
           LEFT JOIN users u ON u.id=r.triggered_by WHERE r.id=?""", (run_id,))
    if not row:
        return None
    d = _run_dict(row)
    items = query(
        """SELECT i.* FROM sync_items i WHERE i.run_id=?
           ORDER BY CASE i.result
             WHEN 'failed' THEN 0 WHEN 'conflict' THEN 1 WHEN 'local_deleted' THEN 2
             WHEN 'upstream_deleted' THEN 3 WHEN 'created' THEN 4 WHEN 'updated' THEN 5
             ELSE 6 END, i.id""", (run_id,))
    d["items"] = [item_dict(r) for r in items]
    return d


def item_dict(r) -> dict:
    d = {
        "id": r["id"], "run_id": r["run_id"],
        "entity_type": r["entity_type"], "entity_label": ENTITY_LABELS[r["entity_type"]],
        "external_key": r["external_key"], "name": r["name"],
        "result": r["result"], "result_label": RESULT_LABELS[r["result"]],
        "reason": r["reason"], "detail": _json_loads(r["detail_json"], default={}),
    }
    return d


def list_conflicts(status: str = "pending") -> list[dict]:
    rows = query(
        """SELECT c.*, u.name AS decided_by_name, r.trigger_type, r.started_at AS run_at
           FROM sync_conflicts c
           LEFT JOIN users u ON u.id=c.decided_by
           LEFT JOIN sync_runs r ON r.id=c.run_id
           WHERE c.status=? ORDER BY c.created_at DESC, c.id DESC""", (status,))
    result = []
    for r in rows:
        d = conflict_detail(dict(r))
        d["decided_by_name"] = r["decided_by_name"]
        d["run_at"] = r["run_at"]
        result.append(d)
    return result


def list_audit(limit: int = 100) -> list[dict]:
    rows = query(
        """SELECT l.*, u.name AS actor_name FROM sync_audit_logs l
           LEFT JOIN users u ON u.id=l.actor_id ORDER BY l.id DESC LIMIT ?""",
        (min(int(limit), 300),))
    action_labels = {
        "trigger": "触发同步", "run_finish": "同步完成", "setting": "同步设置变更",
        "conflict": "冲突裁决", "remote_delete": "上游删除处理", "local_delete": "本地删除",
    }
    out = []
    for r in rows:
        d = dict(r)
        d["action_label"] = action_labels.get(r["action"], r["action"])
        d["actor_name"] = r["actor_name"] or "系统调度"
        d["entity_label"] = ENTITY_LABELS.get(r["entity_type"], "")
        out.append(d)
    return out


def status_overview() -> dict:
    s = settings_row()
    last = query_one("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")
    pending_c = query_one("SELECT COUNT(*) AS c FROM sync_conflicts WHERE status='pending'")["c"]
    pending_d = query_one("SELECT COUNT(*) AS c FROM sync_remote_deletions WHERE status='pending'")["c"]
    running = query_one("SELECT COUNT(*) AS c FROM sync_runs WHERE status='running'")["c"]
    return {
        "settings": {
            "enabled": bool(s["enabled"]), "interval_seconds": s["interval_seconds"],
            "source_scene": s["source_scene"],
            "scene_label": sync_source.SCENE_LABELS.get(s["source_scene"], s["source_scene"]),
        },
        "scenes": [{"value": k, "label": v} for k, v in sync_source.SCENE_LABELS.items()],
        "running": bool(running),
        "pending_conflicts": pending_c,
        "pending_remote_deletions": pending_d,
        "last_run": _run_dict(last) if last else None,
    }
