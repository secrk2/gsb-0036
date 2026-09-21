"""织云系统 - 模拟上游同步源（不连真云）。

上游"周期推过来"的应用 / 环境 / 模块全量数据就落在 sync_source_data 表里，
同步引擎每趟直接读这张表，与真实对接消息队列/开放接口时的读取面一致。

两个演示控制动作：
- reset_source()：源与本地重新对齐（首轮同步全部"无变化"）；
- advance_scenario()：把上游向前演一幕，确定性地制造
  新增 / 单边改动 / 上游删除 / 非法数据 / 双方同改 / 本地已删上游又推 等场景，
  幕间会顺手模拟"本地这段时间有人改过值/删过记录"，让三方比对真实可演。
"""
import time

from .db import CLUSTERS, STATUSES, get_conn, query, query_one

# 模块类型 / 状态白名单（同步引擎与本地 CRUD 共用）
MODULE_TYPES = ["service", "web", "job", "middleware", "database"]
MODULE_TYPE_LABELS = {
    "service": "后端服务", "web": "前端应用", "job": "定时任务",
    "middleware": "中间件", "database": "数据存储",
}
MODULE_STATUSES = ["active", "deprecated", "stopped"]
MODULE_STATUS_LABELS = {"active": "运行中", "deprecated": "已废弃", "stopped": "已停用"}

META_KEY_STAGE = "scenario_stage"


# ---------------------------------------------------------------- 基础读写

def _get_meta(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM sync_meta WHERE key=?", (key,))
    return row["value"] if row else default


def _set_meta(key: str, value: str) -> None:
    get_conn().execute(
        "INSERT INTO sync_meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    get_conn().commit()


def get_stage() -> int:
    try:
        return int(_get_meta(META_KEY_STAGE, "0"))
    except ValueError:
        return 0


def _upsert_source(entity: str, source_id: str, parent: str, name: str,
                   payload: dict, is_deleted: bool, ts: int) -> None:
    import json
    get_conn().execute(
        """INSERT INTO sync_source_data
           (entity, source_id, parent_source_id, name, payload, is_deleted,
            upstream_updated_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(entity, source_id) DO UPDATE SET
             parent_source_id=excluded.parent_source_id,
             name=excluded.name, payload=excluded.payload,
             is_deleted=excluded.is_deleted,
             upstream_updated_at=excluded.upstream_updated_at,
             updated_at=excluded.updated_at""",
        (entity, source_id, parent, name, json.dumps(payload, ensure_ascii=False),
         1 if is_deleted else 0, ts, ts),
    )


def fetch_source() -> list[dict]:
    """同步引擎每趟读取的上游快照（含上游删除标记行）。"""
    import json
    rows = query(
        "SELECT * FROM sync_source_data ORDER BY entity, source_id"
    )
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(r["payload"] or "{}")
        out.append(d)
    return out


def source_counts() -> dict:
    rows = query(
        "SELECT entity, COUNT(*) AS c, SUM(is_deleted) AS d FROM sync_source_data GROUP BY entity"
    )
    counts = {"app": 0, "env": 0, "module": 0,
              "app_deleted": 0, "env_deleted": 0, "module_deleted": 0}
    for r in rows:
        counts[r["entity"]] = r["c"]
        counts[f"{r['entity']}_deleted"] = r["d"] or 0
    return counts


# ---------------------------------------------------------------- 重新对齐

def reset_source(actor_id: int | None = None) -> dict:
    """模拟源与本地重新对齐：本地现有应用/环境/模块全部认领上游身份。

    首轮（reset 后第一趟）同步结果应当全部"无变化"。
    """
    from . import sync_service as sync
    now = int(time.time())
    sync.ensure_settings()
    # 1) 本地记录补 source_id + 基线；每个应用补一个"核心服务"模块，避免模块全是新增
    sync.adopt_local_records(now)
    # 2) 清空模拟源，按本地现状重建上游快照
    conn = get_conn()
    conn.execute("DELETE FROM sync_source_data")
    # 清掉演示遗留的待决/墓碑与同步运行明细，让"重新对齐"是一个干净起点
    conn.execute("DELETE FROM sync_conflicts")
    conn.execute("DELETE FROM sync_tombstones")
    conn.execute("UPDATE applications SET source_deleted=0, sync_upstream_ack_json='{}'")
    conn.execute("UPDATE app_environments SET source_deleted=0, sync_upstream_ack_json='{}'")
    conn.execute("UPDATE app_modules SET source_deleted=0, sync_upstream_ack_json='{}'")
    conn.commit()

    apps = query(
        """SELECT a.id, a.source_id, a.name, b.code AS bl_code, a.cluster,
                  a.environment, a.status, a.description
           FROM applications a JOIN business_lines b ON b.id=a.business_line_id"""
    )
    for a in apps:
        _upsert_source("app", a["source_id"], "", a["name"], {
            "name": a["name"], "business_line_code": a["bl_code"],
            "cluster": a["cluster"], "environment": a["environment"],
            "status": a["status"], "description": a["description"],
        }, False, now)
    envs = query("SELECT source_id, env_key, env_label, app_id FROM app_environments")
    app_src = {a["id"]: a["source_id"] for a in apps}
    for e in envs:
        _upsert_source("env", e["source_id"], app_src.get(e["app_id"], ""), e["env_label"], {
            "env_key": e["env_key"], "env_label": e["env_label"],
        }, False, now)
    mods = query("SELECT source_id, app_id, name, module_type, version_tag, status, description "
                 "FROM app_modules WHERE source_id IS NOT NULL")
    for m in mods:
        _upsert_source("module", m["source_id"], app_src.get(m["app_id"], ""), m["name"], {
            "name": m["name"], "module_type": m["module_type"],
            "version_tag": m["version_tag"], "status": m["status"],
            "description": m["description"],
        }, False, now)
    get_conn().commit()
    _set_meta(META_KEY_STAGE, "0")
    return {"stage": 0, "counts": source_counts()}


# ---------------------------------------------------------------- 场景演进

def _app_source_id_by_name(name: str):
    row = query_one("SELECT source_id FROM applications WHERE name=?", (name,))
    return row["source_id"] if row else None


def _env_source_id(app_name: str, env_key: str):
    row = query_one(
        """SELECT e.source_id FROM app_environments e
           JOIN applications a ON a.id=e.app_id
           WHERE a.name=? AND e.env_key=?""",
        (app_name, env_key),
    )
    return row["source_id"] if row else None


def _module_source_id(app_name: str, module_name: str):
    row = query_one(
        """SELECT m.source_id FROM app_modules m
           JOIN applications a ON a.id=m.app_id
           WHERE a.name=? AND m.name=?""",
        (app_name, module_name),
    )
    return row["source_id"] if row else None


def _update_app_source(source_id: str, fields: dict, ts: int, *, deleted: bool = False) -> None:
    import json
    row = query_one("SELECT name, parent_source_id, payload FROM sync_source_data "
                    "WHERE entity='app' AND source_id=?", (source_id,))
    payload = json.loads(row["payload"]) if row else {}
    payload.update(fields)
    if "name" in fields:
        name = fields["name"]
    else:
        name = row["name"] if row else source_id
    _upsert_source("app", source_id, row["parent_source_id"] if row else "",
                   name, payload, deleted, ts)


def _update_env_source(source_id: str, fields: dict, ts: int, *, deleted: bool = False) -> None:
    import json
    row = query_one("SELECT name, parent_source_id, payload FROM sync_source_data "
                    "WHERE entity='env' AND source_id=?", (source_id,))
    if not row:
        return
    payload = json.loads(row["payload"])
    payload.update(fields)
    _upsert_source("env", source_id, row["parent_source_id"],
                   payload.get("env_label", row["name"]), payload, deleted, ts)


def _update_module_source(source_id: str, fields: dict, ts: int, *, deleted: bool = False) -> None:
    import json
    row = query_one("SELECT name, parent_source_id, payload FROM sync_source_data "
                    "WHERE entity='module' AND source_id=?", (source_id,))
    if not row:
        return
    payload = json.loads(row["payload"])
    payload.update(fields)
    _upsert_source("module", source_id, row["parent_source_id"],
                   payload.get("name", row["name"]), payload, deleted, ts)


def _simulate_local_edit(app_name: str, description: str, actor_id: int, ts: int) -> None:
    """模拟"本地这段时间有人改过值"：只改业务字段，不动同步基线。

    真实的本地编辑路径（应用信息保存）同样不碰 sync_baseline_json，
    因此下一趟三方比对自然会识别出双方同改。
    """
    get_conn().execute(
        "UPDATE applications SET description=?, updated_at=? WHERE name=?",
        (description, ts, app_name),
    )
    app = query_one("SELECT id FROM applications WHERE name=?", (app_name,))
    if app:
        get_conn().execute(
            "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) "
            "VALUES (?,?,?,?,?)",
            (app["id"], actor_id, "信息变更",
             f"上游同步演练：本地侧修改描述为「{description}」", ts),
        )
    get_conn().commit()


def advance_scenario(actor_id: int | None = None) -> dict:
    """把模拟上游向前推一幕，返回本幕说明。"""
    from . import sync_service as sync
    stage = get_stage() + 1
    now = int(time.time())
    if isinstance(actor_id, dict):
        actor_id = actor_id.get("id")
    actor_id = actor_id or (query_one("SELECT id FROM users WHERE username='admin'")["id"])
    changes: list[str] = []

    if stage == 1:
        # —— 第一幕：上游例行推送 ——
        # 1) 全新应用「智能客服平台」（供应链），自带 dev 环境 + 一个模块
        app_src, env_src, mod_src = "app-scenario-cs", "env-scenario-cs-dev", "mod-scenario-cs-core"
        _upsert_source("app", app_src, "", "智能客服平台", {
            "name": "智能客服平台", "business_line_code": "supply",
            "cluster": "华东1集群", "environment": "dev",
            "status": "developing", "description": "上游新登记：智能问答与工单转人工",
        }, False, now)
        _upsert_source("env", env_src, app_src, "开发", {
            "env_key": "dev", "env_label": "开发"}, False, now)
        _upsert_source("module", mod_src, app_src, "会话核心服务", {
            "name": "会话核心服务", "module_type": "service", "version_tag": "v1.0.0",
            "status": "active", "description": "会话编排与问答路由"}, False, now)
        changes.append("上游新增应用「智能客服平台」（含开发环境、会话核心服务模块）")

        # 2) 单边改动：支付网关描述（本地没动 → 应自动落地）
        gw = _app_source_id_by_name("支付网关")
        if gw:
            _update_app_source(gw, {"description": "统一收单、路由与合单支付网关（上游更新：新增合单支付）"}, now)
            changes.append("上游修改「支付网关」描述（本地未改，应自动落地）")

        # 3) 单边改动：会员中心模块升级版本
        m = _module_source_id("会员中心", "核心服务")
        if m:
            _update_module_source(m, {"version_tag": "v2.4.1",
                                      "description": "会员等级、权益与积分账户（上游升级至 v2.4.1）"}, now)
            changes.append("上游把「会员中心 / 核心服务」升级到 v2.4.1（本地未改）")

        # 4) 上游删除应用「对账平台」（本地仍在 → 上游删除待决，不假装没看见）
        dz = _app_source_id_by_name("对账平台")
        if dz:
            _update_app_source(dz, {}, now, deleted=True)
            changes.append("上游删除应用「对账平台」（本地仍挂着，进入上游删除待决）")

        # 5) 非法数据：上游新应用集群不在纳管集群清单
        bad = "app-scenario-bad"
        _upsert_source("app", bad, "", "灰度影子应用", {
            "name": "灰度影子应用", "business_line_code": "pay",
            "cluster": "火星9集群", "environment": "dev",
            "status": "developing", "description": "集群非法的记录，用于演示卡单原因",
        }, False, now)
        changes.append("上游推来一条集群非法的记录「灰度影子应用」（应判为未通过并说明原因）")

    elif stage == 2:
        # —— 第二幕：双方同改 ——
        # 支付网关描述：本地与上游各改各的
        gw = _app_source_id_by_name("支付网关")
        if gw:
            _simulate_local_edit("支付网关", "统一收单与路由网关（本地运维补充：华东双活）", actor_id, now - 60)
            _update_app_source(gw, {"description": "统一收单、路由与合单支付网关（上游更新：接入跨境钱包）"}, now)
            changes.append("「支付网关」描述双方都改了（本地补充华东双活，上游接入跨境钱包）→ 冲突待裁决")

        # 会员中心模块：本地把版本升到 v2.5-本地热修，上游升到 v2.4.2
        m = _module_source_id("会员中心", "核心服务")
        if m:
            row = query_one(
                """SELECT mm.id FROM app_modules mm JOIN applications a ON a.id=mm.app_id
                   WHERE a.name='会员中心' AND mm.name='核心服务'""")
            if row:
                get_conn().execute(
                    "UPDATE app_modules SET version_tag=?, updated_at=? WHERE id=?",
                    ("v2.5.0-hotfix", now - 60, row["id"]))
                get_conn().commit()
            _update_module_source(m, {"version_tag": "v2.4.2"}, now)
            changes.append("「会员中心 / 核心服务」版本本地热修与上游发布冲突 → 待裁决")

        # 本地已删、上游又推：本地删除「积分商城」，同一时刻上游还在更新它
        jf = _app_source_id_by_name("积分商城")
        if jf:
            app = query_one("SELECT id FROM applications WHERE name='积分商城'")
            if app:
                # 走正式删除路径（写墓碑 + 级联），模拟负责人在台账里删了这个应用
                sync.delete_app(app["id"], actor_id,
                                note="上游同步演练：本地侧删除应用", via_scenario=True)
            _update_app_source(jf, {"description": "积分兑换商城（上游仍在维护并推送更新）"}, now)
            changes.append("本地已删除「积分商城」，上游这一趟仍推更新（不得偷偷复活，进入待决）")

    elif stage == 3:
        # —— 第三幕：环境维度的删除与冲突 ——
        gray = _env_source_id("会员中心", "gray")
        if gray:
            _update_env_source(gray, {}, now, deleted=True)
            changes.append("上游删除「会员中心」的灰度环境（本地保留并标记，挂载清点后由人工决定）")

        # 风控引擎：上游状态非法回退 online → developing（状态机只许向前）
        rk = _app_source_id_by_name("风控实时引擎")
        if rk:
            _update_app_source(rk, {"status": "developing"}, now)
            changes.append("上游把「风控实时引擎」状态从上线回退到在研（违反状态机，判未通过）")

        # 上游删除「消息推送中心 / 核心服务」模块
        m = _module_source_id("消息推送中心", "核心服务")
        if m:
            _update_module_source(m, {}, now, deleted=True)
            changes.append("上游删除模块「消息推送中心 / 核心服务」（本地待裁决）")

    else:
        # 第四幕及以后：再来一轮单边更新，演示裁决后的持续同步
        gw = _app_source_id_by_name("支付网关")
        if gw:
            _update_app_source(gw, {"description": f"统一支付网关（上游例行更新 #{stage}）"}, now)
            changes.append(f"上游例行更新「支付网关」描述（第 {stage} 幕）")
        m = _module_source_id("清结算中心", "核心服务")
        if m:
            _update_module_source(m, {"version_tag": f"v3.0.{stage}"}, now)
            changes.append(f"上游发布「清结算中心 / 核心服务」v3.0.{stage}")

    get_conn().commit()
    _set_meta(META_KEY_STAGE, str(stage))
    return {"stage": stage, "changes": changes, "counts": source_counts()}
