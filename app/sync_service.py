"""织云系统 - 上游数据同步领域服务。

三方比对是核心：每条本地记录记住"上次同步成功时的字段快照"（sync_baseline_json），
同步时对比 基线 / 本地 / 上游 三份值：

- 只有上游改了        → 自动落地；
- 只有本地改了        → 保持本地值；
- 两边都改且值不同    → 挂起冲突，摆出逐字段差异等人裁决，绝不静默覆盖；
- 上游标记删除        → 本地不删，标记"上游已删"并挂删除待决；
- 本地删过、上游又推  → 查墓碑，不偷偷复活，挂"本地已删"待决。

裁决（留痕：选了什么、谁定的、何时定的、备注）：
- 双方同改：take_upstream（采用上游）/ keep_local（保留本地，并记住被驳回的上游值，
  同一上游值下一轮不会被自动写回；上游再改成新值时冲突会重新出现）；
- 上游删除：delete_local（级联删除）/ keep_local（保留，忽略后续同一删除标记；上游恢复则复活）；
- 本地已删上游又推：resurrect（恢复）/ keep_deleted（维持删除并永久忽略该源记录）。

每一趟写 sync_runs（触发方式/触发人/起止时间/耗时/汇总）与 sync_items（逐对象结果与未通过原因）。
"""
import json
import re
import threading
import time

from . import env_service as esvc
from .db import (
    BUILTIN_ENV_KEYS, CLUSTERS, ENV_KEY_PATTERN, ENV_LABELS, STATUSES, STATUS_LABELS,
    STATUS_ORDER, TERMINAL_STATUS, get_conn, query, query_one,
)
from .sync_source import MODULE_STATUSES, MODULE_TYPES

APP_FIELD_LABELS = {
    "name": "应用名", "business_line_code": "所属业务线", "cluster": "所属集群",
    "environment": "主环境", "status": "生命周期状态", "description": "描述",
}
MODULE_FIELD_LABELS = {
    "name": "模块名", "module_type": "模块类型", "version_tag": "版本标签",
    "status": "模块状态", "description": "说明",
}
ENV_FIELD_LABELS = {"env_label": "环境名称"}

KIND_LABELS = {
    "both_changed": "双方都改过",
    "upstream_deleted": "上游已删除",
    "local_deleted": "本地已删除，上游仍在推送",
}
RESULT_LABELS = {
    "created": "新增", "updated": "改动", "unchanged": "无变化",
    "conflict": "双方同改待裁决", "upstream_deleted": "上游删除待裁决",
    "local_deleted": "本地已删待裁决", "invalid": "未通过",
    "ignored": "跳过/已忽略",
}

MIN_INTERVAL, MAX_INTERVAL = 30, 86400
_TICK_SECONDS = 5

_run_lock = threading.Lock()
_scheduler_started = False


class SyncValidationError(ValueError):
    """上游单条数据未通过本地规则校验。"""


class SyncRunningError(Exception):
    """上一趟同步还在跑（手动/定时并发时拒绝）。"""


# ---------------------------------------------------------------- 设置

def ensure_settings() -> None:
    get_conn().execute(
        "INSERT OR IGNORE INTO sync_settings (id, enabled, interval_seconds, updated_by, updated_at) "
        "VALUES (1, 0, 300, NULL, ?)",
        (int(time.time()),),
    )
    get_conn().commit()


def get_settings() -> dict:
    ensure_settings()
    r = query_one("SELECT * FROM sync_settings WHERE id=1")
    return {
        "enabled": bool(r["enabled"]),
        "interval_seconds": r["interval_seconds"],
        "updated_by": r["updated_by"],
        "updated_at": r["updated_at"],
    }


def update_settings(enabled: bool, interval_seconds: int, user: dict) -> dict:
    if not (MIN_INTERVAL <= int(interval_seconds) <= MAX_INTERVAL):
        raise ValueError(f"同步间隔需在 {MIN_INTERVAL}–{MAX_INTERVAL} 秒之间")
    ensure_settings()
    get_conn().execute(
        "UPDATE sync_settings SET enabled=?, interval_seconds=?, updated_by=?, updated_at=? WHERE id=1",
        (1 if enabled else 0, int(interval_seconds), user["id"], int(time.time())),
    )
    get_conn().commit()
    return get_settings()


# ---------------------------------------------------------------- 认领 / 快照

def _bl_code_map() -> dict[int, str]:
    return {r["id"]: r["code"] for r in query("SELECT id, code FROM business_lines")}


def _bl_id_by_code() -> dict[str, int]:
    return {r["code"]: r["id"] for r in query("SELECT id, code FROM business_lines")}


def app_comparable(row) -> dict:
    codes = _bl_code_map()
    return {
        "name": row["name"],
        "business_line_code": codes.get(row["business_line_id"], ""),
        "cluster": row["cluster"],
        "environment": row["environment"],
        "status": row["status"],
        "description": row["description"] or "",
    }


def env_comparable(row) -> dict:
    return {"env_label": row["env_label"]}


def module_comparable(row) -> dict:
    return {
        "name": row["name"], "module_type": row["module_type"],
        "version_tag": row["version_tag"] or "", "status": row["status"],
        "description": row["description"] or "",
    }


def _loads(raw: str) -> dict:
    try:
        return json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}


def adopt_local_records(now: int) -> None:
    """给纯本地记录认领上游身份并写入基线（reset/首轮同步前使用，幂等）。"""
    conn = get_conn()
    for a in conn.execute("SELECT * FROM applications WHERE source_id IS NULL").fetchall():
        sid = f"app-local-{a['id']}"
        conn.execute(
            "UPDATE applications SET source_id=? WHERE id=?", (sid, a["id"]))
        conn.execute(
            "UPDATE applications SET sync_baseline_json=? WHERE id=?",
            (json.dumps(app_comparable(a), ensure_ascii=False), a["id"]))
    for e in conn.execute("SELECT * FROM app_environments WHERE source_id IS NULL").fetchall():
        sid = f"env-app{e['app_id']}-{e['env_key']}"
        conn.execute(
            "UPDATE app_environments SET source_id=? WHERE id=?", (sid, e["id"]))
        conn.execute(
            "UPDATE app_environments SET sync_baseline_json=? WHERE id=?",
            (json.dumps(env_comparable(e), ensure_ascii=False), e["id"]))
    # 每个还没有模块的应用补一个"核心服务"，让模块这一类数据开箱可演
    app_ids = [r["id"] for r in conn.execute("SELECT id FROM applications").fetchall()]
    for app_id in app_ids:
        exists = conn.execute(
            "SELECT 1 FROM app_modules WHERE app_id=? LIMIT 1", (app_id,)).fetchone()
        if exists:
            continue
        app = conn.execute("SELECT name FROM applications WHERE id=?", (app_id,)).fetchone()
        conn.execute(
            """INSERT INTO app_modules
               (app_id, source_id, source_deleted, sync_baseline_json, name, module_type,
                version_tag, status, description, created_by, created_at, updated_at)
               VALUES (?,?,0,?,'核心服务','service','v1.0.0','active',?,NULL,?,?)""",
            (app_id, f"mod-app{app_id}-core",
             json.dumps({"name": "核心服务", "module_type": "service", "version_tag": "v1.0.0",
                         "status": "active",
                         "description": f"{app['name']}核心服务（本地初始登记）"}, ensure_ascii=False),
             f"{app['name']}核心服务（本地初始登记）", now, now),
        )
    for m in conn.execute("SELECT * FROM app_modules WHERE source_id IS NULL").fetchall():
        conn.execute(
            "UPDATE app_modules SET source_id=? WHERE id=?",
            (f"mod-local-{m['id']}", m["id"]))
        conn.execute(
            "UPDATE app_modules SET sync_baseline_json=? WHERE id=?",
            (json.dumps(module_comparable(m), ensure_ascii=False), m["id"]))
    conn.commit()


# ---------------------------------------------------------------- 校验归一

def _norm_app_payload(payload: dict) -> dict:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise SyncValidationError("应用名为空")
    bl_code = str(payload.get("business_line_code", "")).strip()
    if bl_code not in _bl_id_by_code():
        raise SyncValidationError(f"业务线编码不存在：{bl_code or '（空）'}")
    cluster = str(payload.get("cluster", "")).strip()
    if cluster not in CLUSTERS:
        raise SyncValidationError(f"集群不在纳管清单：{cluster or '（空）'}，可选：{'、'.join(CLUSTERS)}")
    env = str(payload.get("environment", "")).strip()
    if not re.fullmatch(ENV_KEY_PATTERN, env or ""):
        raise SyncValidationError(f"主环境标识非法：{env or '（空）'}")
    status = str(payload.get("status", "")).strip()
    if status not in STATUSES:
        raise SyncValidationError(f"生命周期状态非法：{status or '（空）'}")
    return {
        "name": name, "business_line_code": bl_code, "cluster": cluster,
        "environment": env, "status": status,
        "description": str(payload.get("description", "") or "").strip(),
    }


def _norm_module_payload(payload: dict) -> dict:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise SyncValidationError("模块名为空")
    mtype = str(payload.get("module_type", "service")).strip()
    if mtype not in MODULE_TYPES:
        raise SyncValidationError(f"模块类型非法：{mtype}")
    status = str(payload.get("status", "active")).strip()
    if status not in MODULE_STATUSES:
        raise SyncValidationError(f"模块状态非法：{status}")
    version = str(payload.get("version_tag", "") or "").strip()[:64]
    return {
        "name": name, "module_type": mtype, "version_tag": version,
        "status": status,
        "description": str(payload.get("description", "") or "").strip()[:500],
    }


def _norm_env_payload(payload: dict) -> dict:
    key = str(payload.get("env_key", "")).strip()
    label = str(payload.get("env_label", "")).strip()
    if not re.fullmatch(ENV_KEY_PATTERN, key or ""):
        raise SyncValidationError(f"环境标识非法：{key or '（空）'}")
    if not label:
        raise SyncValidationError("环境名称为空")
    return {"env_key": key, "env_label": label[:16]}


# ---------------------------------------------------------------- 三方比对

def _diff3(baseline: dict, local: dict, upstream: dict, ack_reject: dict | None = None):
    """返回 (本地改动字段, 上游改动字段, 双方同改字段)。

    ack_reject 记录人工"保留本地"时驳回的上游值：上游仍是该值不算新改动，
    只有上游推来与驳回值不同的新值时才会再次触发冲突。
    """
    ack_reject = ack_reject or {}
    keys = set(baseline) | set(local) | set(upstream)
    local_changed, upstream_changed, both = [], [], []
    for k in sorted(keys):
        lc = local.get(k) != baseline.get(k) and k in local
        uc = upstream.get(k) != baseline.get(k) and k in upstream
        if uc and ack_reject.get(k) is not None and ack_reject.get(k) == upstream.get(k):
            uc = False
        if lc:
            local_changed.append(k)
        if uc:
            upstream_changed.append(k)
        if lc and uc and local.get(k) != upstream.get(k):
            both.append(k)
    return local_changed, upstream_changed, both


def _display_value(entity: str, field: str, value):
    if value is None:
        return None
    if entity == "app":
        if field == "status":
            return STATUS_LABELS.get(value, value)
        if field == "environment":
            return ENV_LABELS.get(value, value)
    if entity == "module":
        from .sync_source import MODULE_STATUS_LABELS, MODULE_TYPE_LABELS
        if field == "status":
            return MODULE_STATUS_LABELS.get(value, value)
        if field == "module_type":
            return MODULE_TYPE_LABELS.get(value, value)
    return value


def _field_diff(entity: str, fields: list[str], baseline: dict, local: dict, upstream: dict):
    labels = {"app": APP_FIELD_LABELS, "module": MODULE_FIELD_LABELS,
              "env": ENV_FIELD_LABELS}[entity]
    out = []
    for f in fields:
        out.append({
            "field": f,
            "label": labels.get(f, f),
            "baseline": _display_value(entity, f, baseline.get(f)),
            "local": _display_value(entity, f, local.get(f)),
            "upstream": _display_value(entity, f, upstream.get(f)),
            "baseline_raw": baseline.get(f),
            "local_raw": local.get(f),
            "upstream_raw": upstream.get(f),
        })
    return out


# ---------------------------------------------------------------- 冲突存取

def _pending_conflict(entity: str, source_id: str):
    return query_one(
        "SELECT * FROM sync_conflicts WHERE entity=? AND source_id=? AND status='pending'",
        (entity, source_id),
    )


def _open_conflict(kind, entity, source_id, parent_source_id, local_id, app_id, run_id,
                   baseline, local, upstream, local_fields, upstream_fields, now) -> int:
    existing = _pending_conflict(entity, source_id)
    if existing:
        # 同一对象仍在待决：更新上游侧最新值与趟次，不重复建行
        get_conn().execute(
            """UPDATE sync_conflicts SET kind=?, parent_source_id=?, local_id=?, app_id=?,
                   run_id=?, detected_at=?, baseline_json=?, local_json=?, upstream_json=?,
                   local_changed_json=?, upstream_changed_json=?
               WHERE id=?""",
            (kind, parent_source_id, local_id, app_id, run_id, now,
             json.dumps(baseline, ensure_ascii=False), json.dumps(local, ensure_ascii=False),
             json.dumps(upstream, ensure_ascii=False),
             json.dumps(local_fields, ensure_ascii=False),
             json.dumps(upstream_fields, ensure_ascii=False),
             existing["id"]))
        get_conn().commit()
        return existing["id"]
    cur = get_conn().execute(
        """INSERT INTO sync_conflicts
           (kind, entity, source_id, parent_source_id, local_id, app_id, run_id, detected_at,
            status, baseline_json, local_json, upstream_json,
            local_changed_json, upstream_changed_json)
           VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)""",
        (kind, entity, source_id, parent_source_id, local_id, app_id, run_id, now,
         json.dumps(baseline, ensure_ascii=False), json.dumps(local, ensure_ascii=False),
         json.dumps(upstream, ensure_ascii=False),
         json.dumps(local_fields, ensure_ascii=False),
         json.dumps(upstream_fields, ensure_ascii=False)),
    )
    get_conn().commit()
    return cur.lastrowid


# ---------------------------------------------------------------- 同步主流程

def _log_item(run_id, entity, source_id, local_id, name, parent_name, result, reason, changes, now):
    if not run_id:
        # 裁决恢复等"趟次外"引擎调用不挂 sync_items（run_id=0 会触发外键约束）
        return
    get_conn().execute(
        """INSERT INTO sync_items
           (run_id, entity, source_id, local_id, name, parent_name, result, reason,
            changes_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (run_id, entity, source_id, local_id, name, parent_name, result, reason,
         json.dumps(changes or [], ensure_ascii=False), now),
    )


def _change_log(app_id, text, now, user_id=None):
    get_conn().execute(
        "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (app_id, user_id, "上游同步", text, now),
    )


def run_sync(trigger_type: str, user: dict | None) -> dict:
    """跑一趟同步。手动/定时共用；进程内单趟锁，并发触发直接拒绝。"""
    from .sync_source import fetch_source
    if not _run_lock.acquire(blocking=False):
        raise SyncRunningError("上一趟同步尚未结束，请稍后再看结果")
    user_id = user["id"] if user else None
    now = int(time.time())
    started = time.monotonic()
    cur = get_conn().execute(
        "INSERT INTO sync_runs (trigger_type, triggered_by, started_at, status) VALUES (?,?,?,'running')",
        (trigger_type, user_id, now),
    )
    get_conn().commit()
    run_id = cur.lastrowid
    totals = {k: 0 for k in
              ("received", "created", "updated", "unchanged", "conflicts",
               "upstream_deleted", "local_deleted", "invalid", "ignored")}
    try:
        rows = fetch_source()
        apps = [r for r in rows if r["entity"] == "app"]
        envs = [r for r in rows if r["entity"] == "env"]
        mods = [r for r in rows if r["entity"] == "module"]
        totals["received"] = len(rows)
        batch_env_keys: dict[str, set[str]] = {}
        for r in envs:
            try:
                p = _norm_env_payload(r["payload"])
                batch_env_keys.setdefault(r["parent_source_id"], set()).add(p["env_key"])
            except SyncValidationError:
                pass

        app_local_id: dict[str, int] = {}
        for r in apps:
            lid = _process_app(r, run_id, now, totals,
                               batch_env_keys.get(r["source_id"], set()))
            if lid:
                app_local_id[r["source_id"]] = lid
        for r in envs:
            _process_env(r, run_id, now, totals, app_local_id)
        for r in mods:
            _process_module(r, run_id, now, totals, app_local_id)

        blocked = (totals["conflicts"] + totals["upstream_deleted"]
                   + totals["local_deleted"] + totals["invalid"])
        status = "partial" if blocked else "success"
        finish_run(run_id, status, totals, None, started)
        return {"run_id": run_id, "status": status, "totals": totals}
    except Exception as exc:  # 整趟级故障：留下 failed 记录与原因
        finish_run(run_id, "failed", totals, f"{type(exc).__name__}: {exc}", started)
        raise
    finally:
        _run_lock.release()


def finish_run(run_id, status, totals, error, started_monotonic):
    now = int(time.time())
    get_conn().execute(
        "UPDATE sync_runs SET status=?, totals_json=?, error=?, finished_at=?, duration_ms=? WHERE id=?",
        (status, json.dumps(totals, ensure_ascii=False), error or "", now,
         int((time.monotonic() - started_monotonic) * 1000), run_id),
    )
    get_conn().commit()


def is_running() -> bool:
    return _run_lock.locked()


# ---------------------------------------------------------------- 应用

def _process_app(row, run_id, now, totals, batch_env_keys) -> int | None:
    sid, name, parent = row["source_id"], row["name"], row["parent_source_id"]
    deleted = bool(row["is_deleted"])
    local = query_one("SELECT * FROM applications WHERE source_id=?", (sid,))
    tomb = query_one("SELECT * FROM sync_tombstones WHERE entity='app' AND source_id=?", (sid,))

    if deleted:
        if not local:
            _log_item(run_id, "app", sid, None, name, "", "ignored",
                      "上游已删除，本地也不存在（双方一致）", [], now)
            totals["ignored"] += 1
            return None
        ack = _loads(local["sync_upstream_ack_json"])
        pending = _pending_conflict("app", sid)
        if ack.get("upstream_deleted_ack") and not pending:
            _log_item(run_id, "app", sid, local["id"], local["name"], "", "ignored",
                      "上游删除标记仍在；此前已裁决保留本地，不再重复打扰", [], now)
            totals["ignored"] += 1
            return local["id"]
        if not bool(local["source_deleted"]):
            get_conn().execute("UPDATE applications SET source_deleted=1 WHERE id=?", (local["id"],))
            get_conn().commit()
        baseline = _loads(local["sync_baseline_json"])
        cid = _open_conflict(
            "upstream_deleted", "app", sid, "", local["id"], local["id"], run_id,
            baseline, app_comparable(local), {}, [], [], now)
        _log_item(run_id, "app", sid, local["id"], local["name"], "", "upstream_deleted",
                  f"上游已删除该应用，本地仍保留（待决冲突 #{cid}）", [], now)
        totals["upstream_deleted"] += 1
        return local["id"]

    # 上游存活
    if tomb and not local:
        if tomb["resolution"] == "ignored":
            _log_item(run_id, "app", sid, None, name, "", "ignored",
                      "本地此前已删除并裁决不恢复，忽略该上游记录", [], now)
            totals["ignored"] += 1
            return None
        cid = _open_conflict(
            "local_deleted", "app", sid, "", None, None, run_id,
            {}, {}, {}, [], [], now)
        _log_item(run_id, "app", sid, None, name, "", "local_deleted",
                  f"本地已删除该应用，上游仍在推送（待决冲突 #{cid}，不会自动复活）", [], now)
        totals["local_deleted"] += 1
        return None

    try:
        upstream = _norm_app_payload(row["payload"])
    except SyncValidationError as e:
        _log_item(run_id, "app", sid, local["id"] if local else None, name, "",
                  "invalid", f"上游数据未通过校验：{e}", [], now)
        totals["invalid"] += 1
        return local["id"] if local else None

    if not local:
        return _create_app(sid, upstream, run_id, now, totals, batch_env_keys)

    # 上游"复活"：此前上游删过、本地裁决保留，现在源端不再删除
    if bool(local["source_deleted"]):
        get_conn().execute(
            "UPDATE applications SET source_deleted=0, sync_upstream_ack_json='{}' WHERE id=?",
            (local["id"],))
        get_conn().commit()
        pend = _pending_conflict("app", sid)
        if pend:
            _resolve_row(pend["id"], "reappeared", None, now, "上游重新推送该应用，自动恢复")
        _change_log(local["id"], "上游此前删除后又恢复推送，应用恢复为正常状态", now)

    bl_codes = _bl_code_map()
    baseline = _loads(local["sync_baseline_json"])
    cur_fields = app_comparable(local)
    ack = _loads(local["sync_upstream_ack_json"])
    if ack.pop("upstream_deleted_ack", None):
        # 上游这一趟活着：此前"拒绝删除、保留本地"的驳回标记作废，以后再删需重新提示
        _update_ack("app", local["id"], ack)
    ack = ack.get("reject", {})
    local_changed, upstream_changed, both = _diff3(baseline, cur_fields, upstream, ack)

    pending = _pending_conflict("app", sid)
    if both:
        cid = pending["id"] if pending else _open_conflict(
            "both_changed", "app", sid, "", local["id"], local["id"], run_id,
            baseline, cur_fields, upstream,
            _field_diff("app", local_changed, baseline, cur_fields, upstream),
            _field_diff("app", upstream_changed, baseline, cur_fields, upstream), now)
        _log_item(run_id, "app", sid, local["id"], local["name"], "", "conflict",
                  f"双方都改过这些字段：{'、'.join(APP_FIELD_LABELS[f] for f in both)}"
                  f"（待决冲突 #{cid}，本地值未被覆盖）",
                  _field_diff("app", sorted(set(local_changed) | set(upstream_changed)),
                              baseline, cur_fields, upstream), now)
        totals["conflicts"] += 1
        return local["id"]
    if pending:
        # 旧待决（如删除类）已在上面处理；值差异待决但上游这次没新内容
        pass
    if not upstream_changed:
        _log_item(run_id, "app", sid, local["id"], local["name"], "", "unchanged", "", [], now)
        totals["unchanged"] += 1
        return local["id"]
    # 只有上游改了：过本地业务规则后落地
    try:
        applied = _apply_app_fields(local, upstream, upstream_changed, batch_env_keys)
    except SyncValidationError as e:
        _log_item(run_id, "app", sid, local["id"], local["name"], "", "invalid",
                  f"上游改动未通过本地规则：{e}", [], now)
        totals["invalid"] += 1
        return local["id"]
    new_baseline = dict(baseline)
    for f in applied:
        new_baseline[f] = upstream[f]
    get_conn().execute(
        "UPDATE applications SET sync_baseline_json=?, updated_at=? WHERE id=?",
        (json.dumps(new_baseline, ensure_ascii=False), now, local["id"]))
    get_conn().commit()
    changes = _field_diff("app", applied, baseline, cur_fields, upstream)
    detail = "上游同步更新：" + "；".join(
        f"{c['label']} {c['baseline'] or '（空）'} → {c['upstream'] or '（空）'}" for c in changes)
    _change_log(local["id"], detail, now)
    _log_item(run_id, "app", sid, local["id"], upstream["name"], "", "updated", "", changes, now)
    totals["updated"] += 1
    return local["id"]


def _create_app(sid, upstream, run_id, now, totals, batch_env_keys) -> int | None:
    bl_id = _bl_id_by_code()[upstream["business_line_code"]]
    dup = query_one("SELECT id FROM applications WHERE business_line_id=? AND name=?",
                    (bl_id, upstream["name"]))
    if dup:
        _log_item(run_id, "app", sid, dup["id"], upstream["name"], "", "invalid",
                  f"业务线「{upstream['business_line_code']}」下已存在同名应用（source_id 不匹配，未自动认领）",
                  [], now)
        totals["invalid"] += 1
        return dup["id"]
    # 上游把应用置为下线等终态时直接落库；新建默认时间戳
    cur = get_conn().execute(
        """INSERT INTO applications
           (name, business_line_id, owner_id, cluster, environment, status, description,
            source_id, source_deleted, sync_baseline_json, created_at, updated_at)
           VALUES (?,?,NULL,?,?,?,?,?,0,?,?,?)""",
        (upstream["name"], bl_id, upstream["cluster"], upstream["environment"],
         upstream["status"], upstream["description"], sid,
         json.dumps(upstream, ensure_ascii=False), now, now),
    )
    app_id = cur.lastrowid
    esvc.ensure_default_environments(app_id, now)
    # 默认环境先不预分配上游身份：本趟显式推送的 env 行按键认领；
    # 上游没推到的标准环境保持纯本地记录（source_id 为 NULL）。
    _change_log(app_id, f"上游同步新增应用（来源 {sid}），负责人待本地指派", now)
    _log_item(run_id, "app", sid, app_id, upstream["name"], "", "created",
              "新增应用，负责人空缺待本地指派",
              _field_diff("app", list(upstream.keys()), {}, {}, upstream), now)
    totals["created"] += 1
    return app_id


def _apply_app_fields(local, upstream, changed, batch_env_keys) -> list[str]:
    """把上游字段应用到本地，过状态机 / 唯一命名 / 环境注册等本地规则。"""
    bl_id = _bl_id_by_code()[upstream["business_line_code"]]
    if "business_line_code" in changed:
        # 业务线迁移涉及权限与归属，演示环境不自动搬，挂规则失败由人处理
        if upstream["business_line_code"] != app_comparable(local)["business_line_code"]:
            raise SyncValidationError("上游变更了应用所属业务线，跨业务线迁移需本地走专门流程，同步不自动执行")
    if "name" in changed and upstream["name"] != local["name"]:
        dup = query_one("SELECT id FROM applications WHERE business_line_id=? AND name=? AND id!=?",
                        (bl_id, upstream["name"], local["id"]))
        if dup:
            raise SyncValidationError(f"业务线下已有同名应用「{upstream['name']}」，拒绝自动更名")
    if "status" in changed:
        old, new = local["status"], upstream["status"]
        if STATUS_ORDER[new] < STATUS_ORDER[old]:
            raise SyncValidationError(
                f"上游要求状态从「{STATUS_LABELS[old]}」回退到「{STATUS_LABELS[new]}」，"
                "生命周期只能向前流转，已拦截")
        if old == TERMINAL_STATUS:
            raise SyncValidationError("应用已在下线终态，上游状态变更被忽略")
    if "environment" in changed and upstream["environment"] != local["environment"]:
        env_exists = query_one(
            "SELECT 1 FROM app_environments WHERE app_id=? AND env_key=?",
            (local["id"], upstream["environment"]))
        if not env_exists and upstream["environment"] not in batch_env_keys:
            raise SyncValidationError(
                f"应用下不存在环境「{upstream['environment']}」，且本趟上游未推送该环境，拒绝切换主环境")
    fields_sql = {"name": "name", "cluster": "cluster", "environment": "environment",
                  "status": "status", "description": "description"}
    for f in changed:
        if f in fields_sql:
            get_conn().execute(
                f"UPDATE applications SET {fields_sql[f]}=? WHERE id=?",
                (upstream[f], local["id"]))
    get_conn().commit()
    return changed


# ---------------------------------------------------------------- 环境

def _parent_pending_tomb(parent_source_id: str):
    """父应用被本地删除且尚未裁决时返回其墓碑行（子对象随父级一起等待，不单独复活）。"""
    if not parent_source_id:
        return None
    return query_one(
        "SELECT * FROM sync_tombstones WHERE entity='app' AND source_id=? AND resolution=''",
        (parent_source_id,))


def _process_env(row, run_id, now, totals, app_local_id) -> None:
    sid, name = row["source_id"], row["name"]
    deleted = bool(row["is_deleted"])
    parent_sid = row["parent_source_id"]
    app = query_one("SELECT * FROM applications WHERE source_id=?", (parent_sid,))
    app_id = app["id"] if app else app_local_id.get(parent_sid)
    parent_name = app["name"] if app else (
        query_one("SELECT name FROM applications WHERE id=?", (app_id,))["name"] if app_id else "")
    parent_tomb = _parent_pending_tomb(parent_sid)
    if parent_tomb:
        _log_item(run_id, "env", sid, None, name, parent_name or parent_tomb["name"], "ignored",
                  "所属应用本地已删除，随父应用的恢复裁决一并处理", [], now)
        totals["ignored"] += 1
        return
    if not app_id:
        _log_item(run_id, "env", sid, None, name, "", "invalid",
                  f"所属应用（{parent_sid or '未知'}）本趟未落库，环境无法挂载", [], now)
        totals["invalid"] += 1
        return
    local = query_one("SELECT * FROM app_environments WHERE source_id=?", (sid,))
    if not local:
        try:
            p = _norm_env_payload(row["payload"])
        except SyncValidationError as e:
            _log_item(run_id, "env", sid, None, name, parent_name, "invalid",
                      f"上游数据未通过校验：{e}", [], now)
            totals["invalid"] += 1
            return
        local = query_one(
            "SELECT * FROM app_environments WHERE app_id=? AND env_key=? AND source_id IS NULL",
            (app_id, p["env_key"]))
        if local:
            # 同应用同键、且尚无上游身份的本地环境：认领上游身份，随后按差异比对
            get_conn().execute(
                "UPDATE app_environments SET source_id=? WHERE id=?", (sid, local["id"]))
            get_conn().commit()
            local = query_one("SELECT * FROM app_environments WHERE id=?", (local["id"],))

    tomb = query_one("SELECT * FROM sync_tombstones WHERE entity='env' AND source_id=?", (sid,))

    if deleted:
        if not local:
            _log_item(run_id, "env", sid, None, name, parent_name, "ignored",
                      "上游已删除，本地也不存在（双方一致）", [], now)
            totals["ignored"] += 1
            return
        ack = _loads(local["sync_upstream_ack_json"])
        pending = _pending_conflict("env", sid)
        if ack.get("upstream_deleted_ack") and not pending:
            _log_item(run_id, "env", sid, local["id"], local["env_label"], parent_name,
                      "ignored", "上游删除标记仍在；此前已裁决保留，不再重复打扰", [], now)
            totals["ignored"] += 1
            return
        if not bool(local["source_deleted"]):
            get_conn().execute("UPDATE app_environments SET source_deleted=1 WHERE id=?", (local["id"],))
            get_conn().commit()
        cid = _open_conflict(
            "upstream_deleted", "env", sid, parent_sid, local["id"], app_id, run_id,
            _loads(local["sync_baseline_json"]), env_comparable(local), {}, [], [], now)
        _log_item(run_id, "env", sid, local["id"], local["env_label"], parent_name,
                  "upstream_deleted",
                  f"上游已删除该环境，本地仍保留（待决冲突 #{cid}；删除前会清点配置/实例挂载）",
                  [], now)
        totals["upstream_deleted"] += 1
        return

    if tomb and not local:
        if tomb["resolution"] == "ignored":
            _log_item(run_id, "env", sid, None, name, parent_name, "ignored",
                      "本地此前已删除并裁决不恢复，忽略该上游记录", [], now)
            totals["ignored"] += 1
            return
        cid = _open_conflict(
            "local_deleted", "env", sid, parent_sid, None, app_id, run_id,
            {}, {}, {}, [], [], now)
        _log_item(run_id, "env", sid, None, name, parent_name, "local_deleted",
                  f"本地已删除该环境，上游仍在推送（待决冲突 #{cid}，不会自动复活）", [], now)
        totals["local_deleted"] += 1
        return

    try:
        p = _norm_env_payload(row["payload"])
    except SyncValidationError as e:
        _log_item(run_id, "env", sid, local["id"] if local else None, name, parent_name,
                  "invalid", f"上游数据未通过校验：{e}", [], now)
        totals["invalid"] += 1
        return

    if not local:
        # 新环境落库（直接注册，与 esvc.create_environment 同构，actor 为系统）
        if query_one("SELECT 1 FROM app_environments WHERE app_id=? AND env_key=?",
                     (app_id, p["env_key"])):
            _log_item(run_id, "env", sid, None, p["env_label"], parent_name, "invalid",
                      "同应用下该环境键已存在", [], now)
            totals["invalid"] += 1
            return
        cur = get_conn().execute(
            """INSERT INTO app_environments
               (app_id, env_key, env_label, is_builtin, deploy_restricted,
                window_days, window_start, window_end, source_id, sync_baseline_json,
                created_by, created_at, updated_at)
               VALUES (?,?,?,0,0,'[]','00:00','23:59',?,?,NULL,?,?)""",
            (app_id, p["env_key"], p["env_label"], sid,
             json.dumps({"env_label": p["env_label"]}, ensure_ascii=False), now, now),
        )
        get_conn().commit()
        env_id = cur.lastrowid
        esvc.log_ops(app_id, p["env_key"], p["env_label"], "env_create", p["env_label"],
                     f"上游同步新增环境「{p['env_label']}」（标识 {p['env_key']}）", None, now)
        _log_item(run_id, "env", sid, env_id, p["env_label"], parent_name, "created", "",
                  _field_diff("env", ["env_label"], {}, {}, {"env_label": p["env_label"]}), now)
        totals["created"] += 1
        return

    if bool(local["source_deleted"]):
        get_conn().execute(
            "UPDATE app_environments SET source_deleted=0, sync_upstream_ack_json='{}' WHERE id=?",
            (local["id"],))
        get_conn().commit()
        pend = _pending_conflict("env", sid)
        if pend:
            _resolve_row(pend["id"], "reappeared", None, now, "上游重新推送该环境，自动恢复")
        esvc.log_ops(app_id, local["env_key"], local["env_label"], "env_create",
                     local["env_label"], "上游此前删除后又恢复推送，环境恢复", None, now)

    baseline = _loads(local["sync_baseline_json"])
    cur_fields = env_comparable(local)
    upstream = {"env_label": p["env_label"]}
    ack_all = _loads(local["sync_upstream_ack_json"])
    if ack_all.pop("upstream_deleted_ack", None):
        _update_ack("env", local["id"], ack_all)
    ack = ack_all.get("reject", {})
    local_changed, upstream_changed, both = _diff3(baseline, cur_fields, upstream, ack)
    if both:
        cid = _open_conflict(
            "both_changed", "env", sid, parent_sid, local["id"], app_id, run_id,
            baseline, cur_fields, upstream,
            _field_diff("env", local_changed, baseline, cur_fields, upstream),
            _field_diff("env", upstream_changed, baseline, cur_fields, upstream), now)
        _log_item(run_id, "env", sid, local["id"], local["env_label"], parent_name, "conflict",
                  f"环境名称双方都改过（待决冲突 #{cid}）",
                  _field_diff("env", ["env_label"], baseline, cur_fields, upstream), now)
        totals["conflicts"] += 1
        return
    if not upstream_changed:
        _log_item(run_id, "env", sid, local["id"], local["env_label"], parent_name,
                  "unchanged", "", [], now)
        totals["unchanged"] += 1
        return
    if p["env_label"] != local["env_label"] and query_one(
            "SELECT 1 FROM app_environments WHERE app_id=? AND env_label=? AND id!=?",
            (app_id, p["env_label"], local["id"])):
        _log_item(run_id, "env", sid, local["id"], local["env_label"], parent_name, "invalid",
                  f"同应用下已有同名环境「{p['env_label']}」", [], now)
        totals["invalid"] += 1
        return
    get_conn().execute(
        "UPDATE app_environments SET env_label=?, sync_baseline_json=?, updated_at=? WHERE id=?",
        (p["env_label"], json.dumps(upstream, ensure_ascii=False), now, local["id"]))
    get_conn().commit()
    esvc.log_ops(app_id, local["env_key"], p["env_label"], "env_create", p["env_label"],
                 f"上游同步更新环境名称：{local['env_label']} → {p['env_label']}", None, now)
    _log_item(run_id, "env", sid, local["id"], p["env_label"], parent_name, "updated", "",
              _field_diff("env", ["env_label"], baseline, cur_fields, upstream), now)
    totals["updated"] += 1


# ---------------------------------------------------------------- 模块

def _process_module(row, run_id, now, totals, app_local_id) -> None:
    sid, name, parent_sid = row["source_id"], row["name"], row["parent_source_id"]
    deleted = bool(row["is_deleted"])
    app = query_one("SELECT * FROM applications WHERE source_id=?", (parent_sid,))
    app_id = app["id"] if app else app_local_id.get(parent_sid)
    parent_name = app["name"] if app else (
        query_one("SELECT name FROM applications WHERE id=?", (app_id,))["name"] if app_id else "")
    parent_tomb = _parent_pending_tomb(parent_sid)
    if parent_tomb:
        _log_item(run_id, "module", sid, None, name, parent_name or parent_tomb["name"], "ignored",
                  "所属应用本地已删除，随父应用的恢复裁决一并处理", [], now)
        totals["ignored"] += 1
        return
    if not app_id:
        _log_item(run_id, "module", sid, None, name, "", "invalid",
                  f"所属应用（{parent_sid or '未知'}）本趟未落库，模块无法挂载", [], now)
        totals["invalid"] += 1
        return
    local = query_one("SELECT * FROM app_modules WHERE source_id=?", (sid,))
    if not local:
        try:
            p = _norm_module_payload(row["payload"])
        except SyncValidationError as e:
            _log_item(run_id, "module", sid, None, name, parent_name, "invalid",
                      f"上游数据未通过校验：{e}", [], now)
            totals["invalid"] += 1
            return
        local = query_one("SELECT * FROM app_modules WHERE app_id=? AND name=?",
                          (app_id, p["name"]))
        if local and local["source_id"]:
            local = None  # 别的 source_id 占用同名，走重名校验
        elif local:
            get_conn().execute("UPDATE app_modules SET source_id=? WHERE id=?", (sid, local["id"]))
            get_conn().commit()
            local = query_one("SELECT * FROM app_modules WHERE id=?", (local["id"],))
    tomb = query_one("SELECT * FROM sync_tombstones WHERE entity='module' AND source_id=?", (sid,))

    if deleted:
        if not local:
            _log_item(run_id, "module", sid, None, name, parent_name, "ignored",
                      "上游已删除，本地也不存在（双方一致）", [], now)
            totals["ignored"] += 1
            return
        ack = _loads(local["sync_upstream_ack_json"])
        pending = _pending_conflict("module", sid)
        if ack.get("upstream_deleted_ack") and not pending:
            _log_item(run_id, "module", sid, local["id"], local["name"], parent_name,
                      "ignored", "上游删除标记仍在；此前已裁决保留，不再重复打扰", [], now)
            totals["ignored"] += 1
            return
        if not bool(local["source_deleted"]):
            get_conn().execute("UPDATE app_modules SET source_deleted=1 WHERE id=?", (local["id"],))
            get_conn().commit()
        cid = _open_conflict(
            "upstream_deleted", "module", sid, parent_sid, local["id"], app_id, run_id,
            _loads(local["sync_baseline_json"]), module_comparable(local), {}, [], [], now)
        _log_item(run_id, "module", sid, local["id"], local["name"], parent_name,
                  "upstream_deleted", f"上游已删除该模块，本地仍保留（待决冲突 #{cid}）", [], now)
        totals["upstream_deleted"] += 1
        return

    if tomb and not local:
        if tomb["resolution"] == "ignored":
            _log_item(run_id, "module", sid, None, name, parent_name, "ignored",
                      "本地此前已删除并裁决不恢复，忽略该上游记录", [], now)
            totals["ignored"] += 1
            return
        cid = _open_conflict(
            "local_deleted", "module", sid, parent_sid, None, app_id, run_id,
            {}, {}, {}, [], [], now)
        _log_item(run_id, "module", sid, None, name, parent_name, "local_deleted",
                  f"本地已删除该模块，上游仍在推送（待决冲突 #{cid}，不会自动复活）", [], now)
        totals["local_deleted"] += 1
        return

    try:
        upstream = _norm_module_payload(row["payload"])
    except SyncValidationError as e:
        _log_item(run_id, "module", sid, local["id"] if local else None, name, parent_name,
                  "invalid", f"上游数据未通过校验：{e}", [], now)
        totals["invalid"] += 1
        return

    if not local:
        if query_one("SELECT 1 FROM app_modules WHERE app_id=? AND name=?",
                     (app_id, upstream["name"])):
            _log_item(run_id, "module", sid, None, upstream["name"], parent_name, "invalid",
                      f"同应用下已存在同名模块「{upstream['name']}」", [], now)
            totals["invalid"] += 1
            return
        cur = get_conn().execute(
            """INSERT INTO app_modules
               (app_id, source_id, source_deleted, sync_baseline_json, name, module_type,
                version_tag, status, description, created_by, created_at, updated_at)
               VALUES (?,?,0,?,?,?,?,?,?,NULL,?,?)""",
            (app_id, sid, json.dumps(upstream, ensure_ascii=False), upstream["name"],
             upstream["module_type"], upstream["version_tag"], upstream["status"],
             upstream["description"], now, now),
        )
        get_conn().commit()
        _change_log(app_id, f"上游同步新增模块「{upstream['name']}」", now)
        _log_item(run_id, "module", sid, cur.lastrowid, upstream["name"], parent_name,
                  "created", "", _field_diff("module", list(upstream.keys()), {}, {}, upstream), now)
        totals["created"] += 1
        return

    if bool(local["source_deleted"]):
        get_conn().execute(
            "UPDATE app_modules SET source_deleted=0, sync_upstream_ack_json='{}' WHERE id=?",
            (local["id"],))
        get_conn().commit()
        pend = _pending_conflict("module", sid)
        if pend:
            _resolve_row(pend["id"], "reappeared", None, now, "上游重新推送该模块，自动恢复")
        _change_log(app_id, f"模块「{local['name']}」随上游恢复推送而恢复", now)

    baseline = _loads(local["sync_baseline_json"])
    cur_fields = module_comparable(local)
    ack_all = _loads(local["sync_upstream_ack_json"])
    if ack_all.pop("upstream_deleted_ack", None):
        _update_ack("module", local["id"], ack_all)
    ack = ack_all.get("reject", {})
    local_changed, upstream_changed, both = _diff3(baseline, cur_fields, upstream, ack)
    if both:
        cid = _open_conflict(
            "both_changed", "module", sid, parent_sid, local["id"], app_id, run_id,
            baseline, cur_fields, upstream,
            _field_diff("module", local_changed, baseline, cur_fields, upstream),
            _field_diff("module", upstream_changed, baseline, cur_fields, upstream), now)
        _log_item(run_id, "module", sid, local["id"], local["name"], parent_name, "conflict",
                  f"双方都改过这些字段：{'、'.join(MODULE_FIELD_LABELS[f] for f in both)}"
                  f"（待决冲突 #{cid}）",
                  _field_diff("module", sorted(set(local_changed) | set(upstream_changed)),
                              baseline, cur_fields, upstream), now)
        totals["conflicts"] += 1
        return
    if not upstream_changed:
        _log_item(run_id, "module", sid, local["id"], local["name"], parent_name,
                  "unchanged", "", [], now)
        totals["unchanged"] += 1
        return
    if "name" in upstream_changed and query_one(
            "SELECT 1 FROM app_modules WHERE app_id=? AND name=? AND id!=?",
            (app_id, upstream["name"], local["id"])):
        _log_item(run_id, "module", sid, local["id"], local["name"], parent_name, "invalid",
                  f"同应用下已有同名模块「{upstream['name']}」", [], now)
        totals["invalid"] += 1
        return
    changes = _field_diff("module", upstream_changed, baseline, cur_fields, upstream)
    get_conn().execute(
        """UPDATE app_modules SET name=?, module_type=?, version_tag=?, status=?, description=?,
               sync_baseline_json=?, updated_at=? WHERE id=?""",
        (upstream["name"], upstream["module_type"], upstream["version_tag"], upstream["status"],
         upstream["description"], json.dumps(upstream, ensure_ascii=False), now, local["id"]))
    get_conn().commit()
    _change_log(app_id, "上游同步更新模块「{0}」：{1}".format(
        local["name"], "；".join(
            f"{c['label']} {c['baseline'] or '（空）'} → {c['upstream'] or '（空）'}" for c in changes)), now)
    _log_item(run_id, "module", sid, local["id"], upstream["name"], parent_name, "updated",
              "", changes, now)
    totals["updated"] += 1


# ---------------------------------------------------------------- 本地删除（写墓碑）

def _write_tombstone(entity, source_id, parent_source_id, name, user_id, now) -> None:
    if not source_id:
        return
    get_conn().execute(
        """INSERT INTO sync_tombstones
           (entity, source_id, parent_source_id, name, deleted_by, deleted_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(entity, source_id) DO UPDATE SET
             deleted_by=excluded.deleted_by, deleted_at=excluded.deleted_at, resolution=''""",
        (entity, source_id, parent_source_id, name, user_id, now),
    )
    get_conn().commit()


def delete_module(module_id: int, user: dict) -> None:
    row = query_one("SELECT m.*, a.name AS app_name, a.source_id AS app_source_id "
                    "FROM app_modules m JOIN applications a ON a.id=m.app_id WHERE m.id=?",
                    (module_id,))
    if not row:
        raise LookupError("模块不存在")
    now = int(time.time())
    if row["source_id"]:
        _write_tombstone("module", row["source_id"], row["app_source_id"] or "",
                         row["name"], user["id"], now)
        _close_pending_on_delete("module", row["source_id"], user, now)
    get_conn().execute("DELETE FROM app_modules WHERE id=?", (module_id,))
    get_conn().commit()
    _change_log(row["app_id"], f"删除模块「{row['name']}」", now, user["id"])


def _close_pending_on_delete(entity, source_id, user, now):
    pend = _pending_conflict(entity, source_id)
    if pend:
        _resolve_row(pend["id"], "local_deleted", user["id"] if user else None, now,
                     "本地侧删除，待决随之关闭")


def app_delete_mounts(app_id: int) -> dict:
    return {
        "config_items": query_one(
            "SELECT COUNT(*) AS c FROM config_items WHERE app_id=?", (app_id,))["c"],
        "config_versions": query_one(
            "SELECT COUNT(*) AS c FROM config_versions WHERE app_id=?", (app_id,))["c"],
        "instances": query_one(
            "SELECT COUNT(*) AS c FROM app_instances WHERE app_id=?", (app_id,))["c"],
        "environments": query_one(
            "SELECT COUNT(*) AS c FROM app_environments WHERE app_id=?", (app_id,))["c"],
        "modules": query_one(
            "SELECT COUNT(*) AS c FROM app_modules WHERE app_id=?", (app_id,))["c"],
    }


def env_delete_blockers(app_id: int, env_id: int) -> list[str]:
    app = query_one("SELECT environment FROM applications WHERE id=?", (app_id,))
    env = query_one("SELECT * FROM app_environments WHERE id=?", (env_id,))
    if not env:
        raise LookupError("环境不存在")
    counts = esvc._usage_counts(app_id, env["env_key"],
                                query_one("SELECT business_line_id FROM applications WHERE id=?",
                                          (app_id,))["business_line_id"])
    blockers = []
    if app and app["environment"] == env["env_key"]:
        blockers.append("该环境是应用当前登记的所属环境，请先调整主环境")
    if counts["config_items"]:
        blockers.append(f"挂着 {counts['config_items']} 个配置项")
    if counts["config_versions"]:
        blockers.append(f"挂着 {counts['config_versions']} 个配置历史版本")
    if counts["instances"]:
        blockers.append(f"挂着 {counts['instances']} 个运行实例")
    return blockers


def delete_app(app_id: int, user: dict | None, note: str = "",
               confirm: bool = False, via_scenario: bool = False) -> dict:
    """删除应用：同步关联对象先写墓碑，再级联删除。有挂载且未确认 → 409 清单。"""
    # 场景脚本直接传用户 id；统一成 {'id': ...} 形态给内部辅助使用
    if user is not None and not isinstance(user, dict):
        user = {"id": user}
    row = query_one("SELECT * FROM applications WHERE id=?", (app_id,))
    if not row:
        raise LookupError(f"应用 #{app_id} 不存在")
    mounts = app_delete_mounts(app_id)
    attached = sum(mounts[k] for k in ("config_items", "instances", "modules"))
    if attached and not confirm and not via_scenario:
        raise esvc.EnvDeleteBlockedError({
            "code": "app_delete_blocked",
            "message": (f"应用「{row['name']}」下还挂着 "
                        f"{mounts['config_items']} 个配置项、{mounts['instances']} 个实例、"
                        f"{mounts['modules']} 个模块；删除会级联清除全部数据，"
                        "请在确认明细后再次提交。"),
            "mounts": mounts,
        })
    now = int(time.time())
    uid = user["id"] if user else None
    # 应用与其全部有 source_id 的子对象写墓碑（上游再推任一条都不会复活）
    children = [("env", r) for r in query(
        "SELECT source_id, env_key, env_label FROM app_environments WHERE app_id=? AND source_id IS NOT NULL",
        (app_id,))]
    children += [("module", r) for r in query(
        "SELECT source_id, name FROM app_modules WHERE app_id=? AND source_id IS NOT NULL",
        (app_id,))]
    for entity, r in children:
        _write_tombstone(entity, r["source_id"], row["source_id"] or "",
                         r["name"] if "name" in r.keys() else r["env_label"], uid, now)
        _close_pending_on_delete(entity, r["source_id"], user, now)
    if row["source_id"]:
        _write_tombstone("app", row["source_id"], "", row["name"], uid, now)
        _close_pending_on_delete("app", row["source_id"], user, now)
    get_conn().execute("DELETE FROM applications WHERE id=?", (app_id,))
    get_conn().commit()
    return {"ok": True, "mounts": mounts, "note": note}


# ---------------------------------------------------------------- 冲突裁决

def _resolve_row(conflict_id: int, resolution: str, user_id: int | None, now: int,
                 note: str) -> None:
    get_conn().execute(
        "UPDATE sync_conflicts SET status='resolved', resolution=?, decided_by=?, decided_at=?, "
        "decision_note=? WHERE id=?",
        (resolution, user_id, now, note, conflict_id),
    )
    get_conn().commit()


def _conflict_or_404(conflict_id: int):
    row = query_one("SELECT * FROM sync_conflicts WHERE id=?", (conflict_id,))
    if not row:
        raise LookupError(f"待裁决事项 #{conflict_id} 不存在")
    return row


def decide_conflict(conflict_id: int, resolution: str, note: str, user: dict) -> dict:
    now = int(time.time())
    row = _conflict_or_404(conflict_id)
    if row["status"] != "pending":
        raise ValueError("该事项已经裁决过")
    note = (note or "").strip()[:200]
    valid = {
        "both_changed": {"take_upstream", "keep_local"},
        "upstream_deleted": {"delete_local", "keep_local"},
        "local_deleted": {"resurrect", "keep_deleted"},
    }[row["kind"]]
    if resolution not in valid:
        raise ValueError(f"非法裁决：{resolution}，可选：{'/'.join(sorted(valid))}")
    app_id = row["app_id"]
    who = f"{user['name']} 裁决"

    if row["kind"] == "both_changed":
        local = _local_row(row["entity"], row["local_id"])
        upstream = _loads(row["upstream_json"])
        if not local:
            raise LookupError("本地记录已不存在，无法按该方案裁决；如需要请选择恢复")
        if resolution == "take_upstream":
            _take_upstream_now(row, local, now)
            detail = f"同步冲突裁决（{who}）：采用上游值。{note}"
        else:
            # 保留本地：记住被驳回的上游值（按字段），同值不再自动写回；基线不动。
            # upstream_changed_json 存的是 _field_diff 产出的字段差异列表。
            rejected = {d["field"]: d.get("upstream_raw")
                        for d in _loads(row["upstream_changed_json"])}
            ack = _loads(local["sync_upstream_ack_json"])
            ack.setdefault("reject", {}).update(rejected)
            _update_ack(row["entity"], local["id"], ack)
            detail = (f"同步冲突裁决（{who}）：保留本地值，驳回上游字段 "
                      f"{'、'.join(rejected.keys())}。{note}")
        _entity_change_log(row["entity"], local, app_id, detail, now, user["id"])
    elif row["kind"] == "upstream_deleted":
        local = _local_row(row["entity"], row["local_id"])
        if resolution == "delete_local":
            _delete_local_now(row, local, user, now)
            _resolve_row(conflict_id, resolution, user["id"], now, note or "裁决删除本地记录")
            return {"ok": True, "resolution": resolution}
        # 保留本地：清标记 + 记住删除驳回；上游若恢复（不再删除）会自动复活
        if local:
            ack = _loads(local["sync_upstream_ack_json"])
            ack["upstream_deleted_ack"] = True
            _update_ack(row["entity"], local["id"], ack)
            _set_source_deleted(row["entity"], local["id"], 0)
            _entity_change_log(
                row["entity"], local, app_id,
                f"同步删除裁决（{who}）：保留本地，不跟随上游删除。{note}", now, user["id"])
    else:  # local_deleted
        if resolution == "keep_deleted":
            get_conn().execute(
                "UPDATE sync_tombstones SET resolution='ignored', decided_by=?, decided_at=? "
                "WHERE entity=? AND source_id=?",
                (user["id"], now, row["entity"], row["source_id"]))
            # 父应用维持删除：随父删除写下的环境/模块墓碑一并忽略，不再逐个挂待决
            if row["entity"] == "app":
                get_conn().execute(
                    "UPDATE sync_tombstones SET resolution='ignored', decided_by=?, decided_at=? "
                    "WHERE entity IN ('env','module') AND parent_source_id=? AND resolution=''",
                    (user["id"], now, row["source_id"]))
            get_conn().commit()
        else:
            _resurrect_now(row, now)
            _entity_change_log(
                row["entity"], None, app_id,
                f"同步删除裁决（{who}）：按上游数据恢复该记录。{note}", now, user["id"])

    _resolve_row(conflict_id, resolution, user["id"], now, note)
    return {"ok": True, "resolution": resolution}


def _local_row(entity, local_id):
    table = {"app": "applications", "env": "app_environments",
             "module": "app_modules"}[entity]
    if not local_id:
        return None
    return query_one(f"SELECT * FROM {table} WHERE id=?", (local_id,))


def _update_ack(entity, local_id, ack: dict) -> None:
    table = {"app": "applications", "env": "app_environments",
             "module": "app_modules"}[entity]
    get_conn().execute(
        f"UPDATE {table} SET sync_upstream_ack_json=? WHERE id=?",
        (json.dumps(ack, ensure_ascii=False), local_id))
    get_conn().commit()


def _set_source_deleted(entity, local_id, value: int) -> None:
    table = {"app": "applications", "env": "app_environments",
             "module": "app_modules"}[entity]
    get_conn().execute(f"UPDATE {table} SET source_deleted=? WHERE id=?", (value, local_id))
    get_conn().commit()


def _take_upstream_now(row, local, now) -> None:
    upstream = _loads(row["upstream_json"])
    if row["entity"] == "app":
        changed = [d["field"] for d in _loads(row["upstream_changed_json"])]
        # 裁决在趟次外发生：从模拟源补出该应用本趟推送的环境键集合，
        # 避免切换主环境时被"环境未注册"误拦
        from .sync_source import fetch_source
        env_keys = {x["payload"].get("env_key") for x in fetch_source()
                    if x["entity"] == "env" and x["parent_source_id"] == row["source_id"]}
        _apply_app_fields(local, upstream, changed, env_keys)
        new_baseline = dict(_loads(local["sync_baseline_json"]))
        new_baseline.update({f: upstream.get(f) for f in changed})
        get_conn().execute(
            "UPDATE applications SET sync_baseline_json=?, sync_upstream_ack_json='{}' WHERE id=?",
            (json.dumps(new_baseline, ensure_ascii=False), local["id"]))
        get_conn().commit()
    elif row["entity"] == "env":
        get_conn().execute(
            "UPDATE app_environments SET env_label=?, sync_baseline_json=?, "
            "sync_upstream_ack_json='{}', updated_at=? WHERE id=?",
            (upstream["env_label"], json.dumps(upstream, ensure_ascii=False), now, local["id"]))
        get_conn().commit()
    else:
        get_conn().execute(
            """UPDATE app_modules SET name=?, module_type=?, version_tag=?, status=?, description=?,
                      sync_baseline_json=?, sync_upstream_ack_json='{}', updated_at=? WHERE id=?""",
            (upstream["name"], upstream["module_type"], upstream["version_tag"],
             upstream["status"], upstream["description"],
             json.dumps(upstream, ensure_ascii=False), now, local["id"]))
        get_conn().commit()


def _delete_local_now(row, local, user, now) -> None:
    if row["entity"] == "app":
        delete_app(local["id"], user, note="上游删除裁决：跟随上游删除", confirm=True)
    elif row["entity"] == "env":
        blockers = env_delete_blockers(local["app_id"], local["id"])
        if blockers:
            raise esvc.EnvDeleteBlockedError({
                "code": "env_delete_blocked",
                "message": "不能跟随上游删除该环境：" + "；".join(blockers) + "。请先解除挂载后再裁决。",
                "blockers": blockers,
            })
        _write_tombstone("env", row["source_id"], row["parent_source_id"],
                         local["env_label"], user["id"], now)
        get_conn().execute("DELETE FROM app_environments WHERE id=?", (local["id"],))
        get_conn().commit()
        esvc.log_ops(local["app_id"], local["env_key"], local["env_label"], "env_delete",
                     local["env_label"], "上游删除裁决：跟随上游删除环境", user["id"], now)
    else:
        app = query_one("SELECT source_id FROM applications WHERE id=?", (local["app_id"],))
        _write_tombstone("module", row["source_id"], app["source_id"] if app else "",
                         local["name"], user["id"], now)
        get_conn().execute("DELETE FROM app_modules WHERE id=?", (local["id"],))
        get_conn().commit()
        _change_log(local["app_id"],
                    f"上游删除裁决：跟随上游删除模块「{local['name']}」", now, user["id"])


def _resurrect_now(row, now) -> None:
    """按上游最新数据恢复本地已删记录。"""
    from .sync_source import fetch_source
    src = next((r for r in fetch_source()
                if r["entity"] == row["entity"] and r["source_id"] == row["source_id"]), None)
    if not src or src["is_deleted"]:
        raise SyncValidationError("上游已不再推送该记录（或也已删除），无法恢复")
    get_conn().execute(
        "DELETE FROM sync_tombstones WHERE entity=? AND source_id=?",
        (row["entity"], row["source_id"]))
    get_conn().commit()
    # 趟次外恢复：run_id 用 NULL（sync_runs 外键可空），不产生挂趟次的明细
    sid_run = row["run_id"]
    totals = {k: 0 for k in
              ("received", "created", "updated", "unchanged", "conflicts",
               "upstream_deleted", "local_deleted", "invalid", "ignored")}
    app_local_id: dict[str, int] = {}
    if row["entity"] == "app":
        _process_app(src, sid_run, now, totals, set())
        app_row = query_one("SELECT id FROM applications WHERE source_id=?", (row["source_id"],))
        if app_row:
            # 恢复父应用 = 一并恢复其子对象：清掉随父应用写下的子墓碑，按上游数据逐行落地
            get_conn().execute(
                "DELETE FROM sync_tombstones WHERE entity IN ('env','module') "
                "AND parent_source_id=?", (row["source_id"],))
            get_conn().commit()
            app_local_id[row["source_id"]] = app_row["id"]
            for s in fetch_source():
                if s["parent_source_id"] != row["source_id"]:
                    continue
                if s["entity"] == "env":
                    _process_env(s, sid_run, now, totals, app_local_id)
                elif s["entity"] == "module":
                    _process_module(s, sid_run, now, totals, app_local_id)
    elif row["entity"] == "env":
        app = query_one("SELECT id FROM applications WHERE source_id=?", (row["parent_source_id"],))
        _process_env(src, sid_run, now, totals,
                     {row["parent_source_id"]: app["id"]} if app else {})
    else:
        app = query_one("SELECT id FROM applications WHERE source_id=?", (row["parent_source_id"],))
        _process_module(src, sid_run, now, totals,
                        {row["parent_source_id"]: app["id"]} if app else {})
    if totals["invalid"]:
        raise SyncValidationError("恢复失败：上游数据未通过本地规则，请在上游修正后再恢复")


def _entity_change_log(entity, local, app_id, text, now, user_id):
    if entity == "env" and local:
        esvc.log_ops(local["app_id"], local["env_key"], local["env_label"],
                     "window_update", local["env_label"], text, user_id, now)
    elif entity == "module" and local:
        _change_log(local["app_id"], text, now, user_id)
    elif entity == "app" and local:
        _change_log(local["id"], text, now, user_id)
    elif app_id:
        _change_log(app_id, text, now, user_id)


# ---------------------------------------------------------------- 查询面

def _record_field_rows(entity: str, record: dict) -> list[dict]:
    """把整条记录转成"字段: 值"行（删除类/本地已删类弹窗展示用）。"""
    labels = {"app": APP_FIELD_LABELS, "module": MODULE_FIELD_LABELS,
              "env": ENV_FIELD_LABELS}[entity]
    order = {"app": ["name", "business_line_code", "cluster", "environment", "status", "description"],
             "module": ["name", "module_type", "version_tag", "status", "description"],
             "env": ["env_label"]}[entity]
    bl_names = {r["code"]: r["name"] for r in query("SELECT code, name FROM business_lines")}
    rows = []
    for f in order:
        if f in record:
            value = record.get(f)
            if entity == "app" and f == "business_line_code":
                value = bl_names.get(value, value)
            rows.append({
                "field": f, "label": labels.get(f, f),
                "baseline": None, "local": value,
                "upstream": None, "local_raw": record.get(f), "upstream_raw": None,
            })
    return rows


def _upstream_fields_from_source(entity: str, source_id: str) -> dict:
    from .sync_source import fetch_source
    row = next((r for r in fetch_source()
                if r["entity"] == entity and r["source_id"] == source_id), None)
    return row["payload"] if row else {}


def _all_source_rows() -> list:
    return query("SELECT entity, source_id, name, payload FROM sync_source_data")


def conflict_to_dict(r) -> dict:
    app = None
    if r["app_id"]:
        app = query_one("SELECT name, business_line_id FROM applications WHERE id=?", (r["app_id"],))
    decider = query_one("SELECT name FROM users WHERE id=?", (r["decided_by"],)) if r["decided_by"] else None
    local_json = _loads(r["local_json"])
    upstream_json = _loads(r["upstream_json"])
    baseline_json = _loads(r["baseline_json"])
    name = local_json.get("name") or local_json.get("env_label") or upstream_json.get("name") or ""
    if not name:
        src = next((x for x in _all_source_rows()
                    if x["entity"] == r["entity"] and x["source_id"] == r["source_id"]), None)
        if src:
            name = src["name"]
    parent_name = ""
    if r["parent_source_id"]:
        p = query_one("SELECT name FROM applications WHERE source_id=?", (r["parent_source_id"],))
        parent_name = p["name"] if p else ""
    # 给前端一份可直接渲染的字段行：双方同改展示本地/上游改动字段的并集；
    # 删除类展示整条记录的内容
    if r["kind"] == "both_changed":
        by_field = {}
        for d in _loads(r["local_changed_json"]) + _loads(r["upstream_changed_json"]):
            by_field.setdefault(d["field"], d)
        field_rows = list(by_field.values())
    elif r["kind"] == "upstream_deleted":
        field_rows = _record_field_rows(r["entity"], local_json)
    else:
        # local_deleted：本地已无记录，从模拟源读上游当前值用于展示
        src_fields = _upstream_fields_from_source(r["entity"], r["source_id"])
        field_rows = _record_field_rows(r["entity"], src_fields)
    mounts = None
    blockers = None
    if r["kind"] == "upstream_deleted" and r["status"] == "pending":
        if r["entity"] == "app" and r["local_id"]:
            mounts = app_delete_mounts(r["local_id"])
        elif r["entity"] == "env" and r["local_id"]:
            try:
                blockers = env_delete_blockers(r["app_id"], r["local_id"])
            except LookupError:
                blockers = ["环境记录已不存在"]
    return {
        "id": r["id"], "kind": r["kind"], "kind_label": KIND_LABELS[r["kind"]],
        "entity": r["entity"], "source_id": r["source_id"],
        "local_id": r["local_id"], "app_id": r["app_id"],
        "app_name": app["name"] if app else parent_name or "（应用已删除）",
        "name": name, "parent_name": parent_name,
        "run_id": r["run_id"], "detected_at": r["detected_at"],
        "status": r["status"],
        "baseline": baseline_json, "local": local_json, "upstream": upstream_json,
        "local_changes": _loads(r["local_changed_json"]),
        "upstream_changes": _loads(r["upstream_changed_json"]),
        "field_rows": field_rows,
        "resolution": r["resolution"], "decision_note": r["decision_note"],
        "decided_by_name": decider["name"] if decider else None,
        "decided_at": r["decided_at"],
        "delete_mounts": mounts, "delete_blockers": blockers,
    }


def list_conflicts(status: str = "pending") -> list[dict]:
    if status == "all":
        rows = query("SELECT * FROM sync_conflicts ORDER BY "
                     "CASE status WHEN 'pending' THEN 0 ELSE 1 END, detected_at DESC, id DESC LIMIT 200")
    else:
        rows = query("SELECT * FROM sync_conflicts WHERE status=? ORDER BY detected_at DESC, id DESC LIMIT 200",
                     (status,))
    return [conflict_to_dict(r) for r in rows]


def pending_counts() -> dict:
    rows = query(
        "SELECT kind, COUNT(*) AS c FROM sync_conflicts WHERE status='pending' GROUP BY kind")
    out = {"both_changed": 0, "upstream_deleted": 0, "local_deleted": 0, "total": 0}
    for r in rows:
        out[r["kind"]] = r["c"]
        out["total"] += r["c"]
    return out


def run_to_dict(r, include_items: bool = False) -> dict:
    totals = _loads(r["totals_json"])
    d = {
        "id": r["id"], "trigger_type": r["trigger_type"],
        "trigger_label": "手动触发" if r["trigger_type"] == "manual" else "定时调度",
        "triggered_by": r["triggered_by"],
        "triggered_by_name": None,
        "started_at": r["started_at"], "finished_at": r["finished_at"],
        "duration_ms": r["duration_ms"], "status": r["status"],
        "status_label": {"running": "进行中", "success": "全部完成",
                         "partial": "有待处理项", "failed": "执行失败"}.get(r["status"], r["status"]),
        "totals": totals, "error": r["error"],
    }
    if r["triggered_by"]:
        u = query_one("SELECT name FROM users WHERE id=?", (r["triggered_by"],))
        d["triggered_by_name"] = u["name"] if u else None
    else:
        d["triggered_by_name"] = "系统定时调度"
    if include_items:
        items = query("SELECT * FROM sync_items WHERE run_id=? ORDER BY id", (r["id"],))
        d["items"] = [item_to_dict(x) for x in items]
    return d


def item_to_dict(r) -> dict:
    return {
        "id": r["id"], "entity": r["entity"], "source_id": r["source_id"],
        "local_id": r["local_id"], "name": r["name"], "parent_name": r["parent_name"],
        "result": r["result"], "result_label": RESULT_LABELS.get(r["result"], r["result"]),
        "reason": r["reason"], "changes": _loads(r["changes_json"]),
        "created_at": r["created_at"],
    }


def list_runs(limit: int = 20) -> list[dict]:
    rows = query("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,))
    return [run_to_dict(r) for r in rows]


def get_run(run_id: int) -> dict:
    row = query_one("SELECT * FROM sync_runs WHERE id=?", (run_id,))
    if not row:
        raise LookupError(f"同步趟次 #{run_id} 不存在")
    return run_to_dict(row, include_items=True)


def last_run():
    row = query_one("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")
    return run_to_dict(row) if row else None


def status_overview() -> dict:
    from . import sync_source as src
    settings = get_settings()
    last = last_run()
    next_at = None
    now = int(time.time())
    if settings["enabled"] and last and last["finished_at"]:
        next_at = max(last["started_at"] + settings["interval_seconds"], now + _TICK_SECONDS)
    elif settings["enabled"]:
        next_at = now + _TICK_SECONDS
    return {
        "settings": settings,
        "running": is_running(),
        "last_run": last,
        "next_run_at": next_at,
        "pending": pending_counts(),
        "source": {
            "stage": src.get_stage(),
            "counts": src.source_counts(),
        },
    }


# ---------------------------------------------------------------- 模块本地 CRUD

def list_modules(app_id: int) -> list[dict]:
    from .sync_source import MODULE_STATUS_LABELS, MODULE_TYPE_LABELS
    rows = query("SELECT * FROM app_modules WHERE app_id=? ORDER BY id", (app_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["module_type_label"] = MODULE_TYPE_LABELS.get(r["module_type"], r["module_type"])
        d["status_label"] = MODULE_STATUS_LABELS.get(r["status"], r["status"])
        d["from_upstream"] = bool(r["source_id"])
        out.append(d)
    return out


def _validate_module_fields(app_id: int, fields: dict, module_id: int | None = None) -> dict:
    name = fields["name"].strip()
    if not name:
        raise ValueError("模块名不能为空")
    mtype = fields.get("module_type", "service")
    if mtype not in MODULE_TYPES:
        raise ValueError(f"模块类型非法：{mtype}")
    status = fields.get("status", "active")
    if status not in MODULE_STATUSES:
        raise ValueError(f"模块状态非法：{status}")
    dup = query_one("SELECT id FROM app_modules WHERE app_id=? AND name=? AND id IS NOT ?",
                    (app_id, name, module_id))
    if dup:
        raise ValueError(f"同应用下模块名「{name}」已存在")
    return {
        "name": name, "module_type": mtype,
        "version_tag": fields.get("version_tag", "").strip()[:64],
        "status": status,
        "description": fields.get("description", "").strip()[:500],
    }


def create_module(app_id: int, fields: dict, user: dict) -> dict:
    if not query_one("SELECT 1 FROM applications WHERE id=?", (app_id,)):
        raise LookupError("应用不存在")
    data = _validate_module_fields(app_id, fields)
    now = int(time.time())
    cur = get_conn().execute(
        """INSERT INTO app_modules
           (app_id, source_id, source_deleted, sync_baseline_json, name, module_type,
            version_tag, status, description, created_by, created_at, updated_at)
           VALUES (?,?,0,'{}',?,?,?,?,?,?,?,?)""",
        (app_id, None, data["name"], data["module_type"], data["version_tag"],
         data["status"], data["description"], user["id"], now, now),
    )
    get_conn().commit()
    _change_log(app_id, f"本地新增模块「{data['name']}」", now, user["id"])
    return dict(query_one("SELECT * FROM app_modules WHERE id=?", (cur.lastrowid,)))


def update_module(module_id: int, fields: dict, user: dict) -> dict:
    row = query_one("SELECT * FROM app_modules WHERE id=?", (module_id,))
    if not row:
        raise LookupError("模块不存在")
    data = _validate_module_fields(row["app_id"], fields, module_id)
    now = int(time.time())
    # 同步纳管的模块：本地编辑不碰基线，下一趟三方比对会识别"本地改过"
    get_conn().execute(
        """UPDATE app_modules SET name=?, module_type=?, version_tag=?, status=?, description=?,
               updated_at=? WHERE id=?""",
        (data["name"], data["module_type"], data["version_tag"], data["status"],
         data["description"], now, module_id),
    )
    get_conn().commit()
    _change_log(row["app_id"], f"本地编辑模块「{row['name']}」", now, user["id"])
    return dict(query_one("SELECT * FROM app_modules WHERE id=?", (module_id,)))


# ---------------------------------------------------------------- 定时调度

def start_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    thread = threading.Thread(target=_scheduler_loop, name="sync-scheduler", daemon=True)
    thread.start()


def _scheduler_loop() -> None:
    from .sync_source import fetch_source
    while True:
        time.sleep(_TICK_SECONDS)
        try:
            settings = get_settings()
            if not settings["enabled"] or is_running():
                continue
            if not fetch_source():
                # 模拟源还没初始化（未跑种子/reset），空转不产生空趟次
                continue
            last = query_one("SELECT started_at FROM sync_runs ORDER BY id DESC LIMIT 1")
            last_ts = last["started_at"] if last else 0
            if int(time.time()) - last_ts >= settings["interval_seconds"]:
                try:
                    run_sync("scheduled", None)
                except SyncRunningError:
                    pass
                except Exception as exc:  # 调度线程不能因单趟故障退出
                    print(f"[sync-scheduler] 定时同步失败: {exc}")
        except Exception as exc:  # pragma: no cover - 守护线程兜底
            print(f"[sync-scheduler] 调度循环异常: {exc}")
