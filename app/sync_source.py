"""织云系统 - 模拟上游同步源（不连真实云，全部数据在本地表里演化）。

上游"系统"自己的库 = sync_source_records（推送报文）+ 删除标记。
同步服务每一趟通过 fetch_batch() 拉取当前报文；不同剧本（scene）模拟上游的不同状态：

- steady    常态：报文稳定，多跑几趟应当全部"无变化"；
- rolling   滚动更新：上游每趟真改一个值（描述里带趟次），用来持续演示"改动"；
- conflict  撞车：在滚动之上再推一条与本地改动撞车的记录，演示三方对比与人工裁决；
- invalid   异常报文：夹两条过不了本地校验的数据，演示"没通过的几条卡在哪"。

另外恒定保留两类删除演示报文（任何剧本都在）：
- 本地早已删除（有墓碑）、上游却还在推的记录 —— 不能偷偷复活；
- 上游已删（报文里消失）、本地仍挂着的应用 —— 不能假装没看见。
"""
import json
import time

from .db import execute, get_conn, query, query_one

SCENE_LABELS = {
    "steady": "常态推送",
    "rolling": "滚动更新",
    "conflict": "改动撞车",
    "invalid": "异常报文",
}
SCENES = list(SCENE_LABELS)

# 业务线通过 code 对账；这些 code 与 seed.py 里的四条业务线一致
def _app(name, bl, cluster="华东1集群", environment="dev",
         status="developing", description="") -> dict:
    return {
        "name": name, "business_line": bl, "cluster": cluster,
        "environment": environment, "status": status, "description": description,
    }


# (entity_type, external_key, parent_app_key, payload, is_deleted)
FIXTURES = [
    # ---- 正常会被同步进来的应用 ----
    ("app", "ext-app-pay-notify", None,
     _app("支付通知服务", "pay", description="统一支付结果异步通知与重试"), False),
    ("app", "ext-app-growth-coupon", None,
     _app("优惠券中心", "growth", cluster="华南1集群", environment="staging",
          description="优惠券发放、核销与对账"), False),
    # 冲突演示：基线描述固定；本地预置了一条"本地修订值"，conflict 剧本上游再推"上游修订值"
    ("app", "ext-app-conflict", None,
     _app("冲突演示应用", "pay", description="基线描述（演示三方合并）"), False),
    # 上游已删除：报文里没有它（is_deleted=1，fetch 时跳过），但本地预置了对应应用
    ("app", "ext-app-remdel", None,
     _app("上游已删演示应用", "pay", environment="prod", status="online",
          description="上游 CMDB 已下线该应用"), True),
    # 本地已删：上游仍在推，本地用墓碑挡住复活
    ("app", "ext-app-local-gone", None,
     _app("本地已删演示应用", "pay", description="本地评审未通过，已删除并拒收上游数据"), False),

    # ---- 模块 ----
    ("module", "ext-mod-notify-core", "ext-app-pay-notify",
     {"name": "通知下发核心", "module_type": "service",
      "description": "通知通道选择与重试队列"}, False),
    ("module", "ext-mod-notify-job", "ext-app-pay-notify",
     {"name": "通知对账定时任务", "module_type": "job",
      "description": "T+1 通知成功率对账"}, False),
    # 本地已删的模块：上游仍在推
    ("module", "ext-mod-tomb", "ext-app-pay-notify",
     {"name": "旧短信通道模块", "module_type": "service",
      "description": "已被新通道替代，本地早前删除"}, False),

    # ---- 环境（挂在支付通知服务下的自定义环境）----
    ("environment", "ext-app-pay-notify#gray-demo", "ext-app-pay-notify",
     {"env_key": "gray-demo", "env_label": "灰度演练"}, False),
]


def ensure_source_seeded() -> None:
    """幂等写入上游模拟报文与本地演示实体（启动时调用）。"""
    now = int(time.time())
    if query_one("SELECT 1 FROM sync_source_records LIMIT 1"):
        return
    for entity_type, key, parent, payload, is_deleted in FIXTURES:
        execute(
            """INSERT INTO sync_source_records
               (entity_type, external_key, parent_app_key, payload_json,
                is_deleted, version, created_at, updated_at)
               VALUES (?,?,?,?,?,1,?,?)""",
            (entity_type, key, parent, json.dumps(payload, ensure_ascii=False),
             1 if is_deleted else 0, now, now),
        )
    _ensure_local_demo(now)


def _ensure_local_demo(now: int) -> None:
    """在本地侧预置三种"光跑一趟看不到、但需求必须覆盖"的演示状态。

    1. 冲突演示应用：external_id 已绑定，基线是旧值，本地已被人改过；
    2. 上游已删演示应用：本地真实存在的在线应用（随后由同步趟次发现上游没了）；
    3. 两条墓碑：本地已删的应用/模块，上游还在推。
    """
    from . import env_service as esvc

    bl = query_one("SELECT id FROM business_lines WHERE code='pay'")
    if not bl:
        return
    bl_id = bl["id"]

    def ensure_app(ext_key, name, env, status, desc):
        row = query_one("SELECT id FROM applications WHERE external_id=?", (ext_key,))
        if row:
            return row["id"]
        # 同名占位（旧库手工建过）也复用，不重复造
        row = query_one(
            "SELECT id FROM applications WHERE business_line_id=? AND name=?", (bl_id, name)
        )
        if row:
            execute("UPDATE applications SET external_id=? WHERE id=?", (ext_key, row["id"]))
            return row["id"]
        cur = execute(
            """INSERT INTO applications
               (name, business_line_id, owner_id, cluster, environment, status,
                description, created_at, updated_at, external_id, baseline_payload)
               VALUES (?,?,NULL,'华东1集群',?,?,'',?,?,?,?)""",
            (name, bl_id, env, status, now, now, ext_key,
             json.dumps(_app(name, "pay", environment=env, status=status),
                        ensure_ascii=False)),
        )
        esvc.ensure_default_environments(cur.lastrowid, now)
        return cur.lastrowid

    # 1) 冲突演示：基线旧描述，本地已改成"本地修订值"
    conflict_id = ensure_app("ext-app-conflict", "冲突演示应用", "dev",
                             "developing", "本地修订值（本地这段时间改过）")
    baseline = _app("冲突演示应用", "pay", description="基线描述（演示三方合并）")
    execute(
        "UPDATE applications SET description=?, baseline_payload=? WHERE id=?",
        ("本地修订值（本地这段时间改过）", json.dumps(baseline, ensure_ascii=False), conflict_id),
    )

    # 2) 上游已删：本地仍在线。不预登记删除台账——由首趟同步发现报文里没有它时登记，
    #    这样"首次发现趟次/时间"就是真实的第 1 趟而不是第 0 趟。
    ensure_app("ext-app-remdel", "上游已删演示应用", "prod",
               "online", "上游 CMDB 已下线该应用")

    # 3a) 应用墓碑（本地已删，上游还推 ext-app-local-gone）
    execute(
        """INSERT OR IGNORE INTO sync_tombstones
           (entity_type, external_key, name, deleted_by, deleted_at)
           VALUES ('app','ext-app-local-gone','本地已删演示应用',NULL,?)""",
        (now,),
    )
    # 3b) 模块墓碑：墓碑表不挂父应用外键，无条件就位（父应用由同步首趟创建）
    execute(
        """INSERT OR IGNORE INTO sync_tombstones
           (entity_type, external_key, name, deleted_by, deleted_at)
           VALUES ('module','ext-mod-tomb','旧短信通道模块',NULL,?)""",
        (now,),
    )


def _bump_rolling(tick: int) -> None:
    """rolling/conflict 剧本：上游真改一个值（持久化进上游报文），趟次写进描述。"""
    row = query_one(
        "SELECT payload_json, version FROM sync_source_records WHERE external_key='ext-app-pay-notify'"
    )
    payload = json.loads(row["payload_json"])
    payload["description"] = f"统一支付结果异步通知与重试（上游第 {tick} 次修订）"
    execute(
        "UPDATE sync_source_records SET payload_json=?, version=?, updated_at=? WHERE external_key='ext-app-pay-notify'",
        (json.dumps(payload, ensure_ascii=False), tick, int(time.time())),
    )


def fetch_batch(scene: str) -> list[dict]:
    """返回这一趟上游推送的全部记录（已删除的不推）。

    每条：{entity_type, external_key, parent_app_key, payload, version, updated_at}
    """
    scene = scene if scene in SCENES else "steady"
    tick_row = query_one("SELECT COALESCE(MAX(version),0) AS v FROM sync_source_records")
    tick = (tick_row["v"] or 0) + 1 if scene in ("rolling", "conflict") else 0
    if scene in ("rolling", "conflict"):
        _bump_rolling(tick)

    rows = query(
        """SELECT entity_type, external_key, parent_app_key, payload_json, version, updated_at
           FROM sync_source_records WHERE is_deleted=0 ORDER BY
           CASE entity_type WHEN 'app' THEN 0 WHEN 'module' THEN 1 ELSE 2 END, id"""
    )
    batch = []
    for r in rows:
        item = {
            "entity_type": r["entity_type"],
            "external_key": r["external_key"],
            "parent_app_key": r["parent_app_key"],
            "payload": json.loads(r["payload_json"]),
            "version": r["version"],
            "updated_at": r["updated_at"],
        }
        if scene == "conflict" and r["external_key"] == "ext-app-conflict":
            item["payload"] = dict(item["payload"])
            item["payload"]["description"] = "上游修订值（与本地改动撞车，需要人工裁决）"
            item["updated_at"] = int(time.time())
        batch.append(item)

    if scene == "invalid":
        # 两条过不了本地校验的报文：非法集群 / 挂在一个上游不存在的应用下
        batch.append({
            "entity_type": "app", "external_key": "ext-app-bad",
            "parent_app_key": None, "version": 1, "updated_at": int(time.time()),
            "payload": _app("异常演示应用", "pay", cluster="火星9集群",
                            description="集群编码不存在，应当被本地校验拦下"),
        })
        batch.append({
            "entity_type": "module", "external_key": "ext-mod-orphan",
            "parent_app_key": "ext-app-never-exists", "version": 1,
            "updated_at": int(time.time()),
            "payload": {"name": "孤儿模块", "module_type": "service",
                        "description": "所属应用上游与本地都不存在"},
        })
    return batch


def reset_demo() -> None:
    """把同步演示恢复到"从没同步过"的状态（管理端"重置演示数据"用）。

    清掉本地 ext-% 同步实体与全部同步队列，再重新种报文与本地演示状态。
    应用级联删除会带走其环境、模块、配置与实例。
    """
    conn = get_conn()
    conn.execute("DELETE FROM applications WHERE external_id LIKE 'ext-%'")
    conn.execute("DELETE FROM sync_modules WHERE external_id LIKE 'ext-%'")
    conn.execute("DELETE FROM app_environments WHERE external_id LIKE 'ext-%'")
    conn.execute("DELETE FROM sync_source_records")
    conn.execute("DELETE FROM sync_tombstones WHERE external_key LIKE 'ext-%'")
    conn.execute("DELETE FROM sync_remote_deletions")
    conn.execute("DELETE FROM sync_conflicts")
    conn.execute("DELETE FROM sync_items")
    conn.execute("DELETE FROM sync_runs")
    conn.commit()
    ensure_source_seeded()
