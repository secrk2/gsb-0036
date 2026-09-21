"""初始数据：4 条业务线、10 个用户（四类角色）、24 个应用（覆盖 在研/上线/维保/下线 全部状态）。

权限演示样例：
- 周婷（只读观察者）：仅被授予「数据平台 · 生产」配置查看权（脱敏），不能编辑、不能看明文；
- 钱一（只读观察者）：仅能看「用户增长 · 生产」，且单独授予密文查看权 —— 能看明文但不能改；
- 陈晨（应用负责人）：能编辑自己负责的应用，但生产环境默认没有密文查看权（编辑权 ≠ 明文权）；
- 含一条应用交接留痕（周婷转岗，离线调度平台移交孙磊，配置随应用一并移交）。

部分应用故意缺失负责人，用于控制台红点提示演示。
变更日志时间相对启动时刻生成，保证"近 7 天有变更"开箱即有数据。
"""
import json
import time

from . import env_service as esvc
from .db import ENV_LABELS, STATUS_LABELS, execute, query_one

DAY = 86400

BUSINESS_LINES = [
    ("支付结算", "pay"),
    ("用户增长", "growth"),
    ("供应链", "supply"),
    ("数据平台", "data"),
]

# (username, 姓名, 角色, 业务线 code 或 None)
# admin 平台管理员 / bl_owner 业务线负责人 / app_owner 应用负责人 / viewer 只读观察者
USERS = [
    ("admin",    "系统管理员", "admin",     None),
    ("zhangwei", "张伟", "bl_owner",  "pay"),
    ("lina",     "李娜", "app_owner", "pay"),
    ("wangqiang", "王强", "app_owner", "growth"),
    ("chenchen", "陈晨", "app_owner", "growth"),
    ("qianyi",   "钱一", "viewer",    "growth"),
    ("liuyang",  "刘洋", "bl_owner",  "supply"),
    ("zhaomin",  "赵敏", "app_owner", "supply"),
    ("sunlei",   "孙磊", "app_owner", "data"),
    ("zhouting", "周婷", "viewer",    "data"),
]

# (被授权人 username, 业务线 code, 环境 '*'或具体, 可编辑, 可看明文, 授权人 username, 距今天数)
# 查看权随授予默认开放；密文查看权与编辑权分别给。
GRANTS = [
    # 李娜：支付结算生产环境 编辑+密文；预发环境只给编辑（密文权仍独立）
    ("lina", "pay", "*",       1, 1, "admin", 6),
    # 王强：用户增长生产 编辑+密文
    ("wangqiang", "growth", "prod", 1, 1, "admin", 6),
    # 陈晨：预发可看明文；生产默认无密文权（她负责的应用也能改，但看不到生产明文）
    ("chenchen", "growth", "staging", 1, 1, "admin", 4),
    # 赵敏：供应链生产 编辑+密文
    ("zhaomin", "supply", "prod", 1, 1, "liuyang", 7),
    # 孙磊：数据平台全部环境 编辑+密文
    ("sunlei", "data", "*", 1, 1, "admin", 6),
    # 周婷：只读观察者，仅数据平台·生产，脱敏查看，不能编辑、不能看明文
    ("zhouting", "data", "prod", 0, 0, "admin", 10),
    # 钱一：只读观察者，仅用户增长·生产；授予查看 + 密文明文，但角色封顶永远不能编辑
    # —— 用来演示"只能看某条业务线的生产环境"以及"能看明文 ≠ 能改"
    ("qianyi", "growth", "prod", 0, 1, "admin", 3),
]

# (应用名, 业务线, 负责人 username 或 None, 集群, 环境, 状态, 描述, 环境变量数, 距今天数)
APPS = [
    # 支付结算
    ("支付网关",        "pay",    "zhangwei", "华东1集群", "prod",    "online",      "统一收单与路由网关", 5, 1),
    ("清结算中心",      "pay",    "lina",     "华东1集群", "prod",    "maintenance", "T+1 清分结算批处理", 4, 3),
    ("风控实时引擎",    "pay",    "zhangwei", "华北2集群", "prod",    "online",      "实时交易风控决策", 6, 6),
    ("对账平台",        "pay",    None,       "华南1集群", "test",    "developing",  "渠道对账与差错处理", 0, 2),
    ("收银台 H5",       "pay",    "lina",     "华东1集群", "staging", "online",      "移动端收银台", 3, 20),
    ("代付通道服务",    "pay",    None,       "华北2集群", "prod",    "offline",     "已迁移至新代付平台", 2, 40),
    # 用户增长
    ("会员中心",        "growth", "wangqiang", "华东1集群", "prod",   "online",      "会员等级与权益", 5, 4),
    ("裂变活动平台",    "growth", "chenchen", "华南1集群", "staging", "developing",  "老带新裂变活动配置", 0, 0),
    ("消息推送中心",    "growth", "wangqiang", "华北2集群", "prod",   "maintenance", "Push/短信/站内信", 4, 5),
    ("积分商城",        "growth", None,        "华东1集群", "test",   "developing",  "积分兑换商城", 3, 12),
    ("增长实验平台",    "growth", "chenchen", "华北2集群", "dev",     "developing",  "AB 实验与分流", 0, 1),
    ("老客召回系统",    "growth", "wangqiang", "华南1集群", "prod",   "offline",     "已被消息推送中心替代", 1, 60),
    # 供应链
    ("订单履约中心",    "supply", "liuyang",  "华东1集群", "prod",    "online",      "订单寻源与履约调度", 6, 2),
    ("仓储管理 WMS",    "supply", "zhaomin",  "华北2集群", "prod",    "maintenance", "仓内作业管理", 5, 8),
    ("运输调度 TMS",    "supply", "liuyang",  "华南1集群", "staging", "online",      "干线与城配调度", 4, 15),
    ("供应商门户",      "supply", None,       "华东1集群", "test",    "developing",  "供应商协同门户", 0, 3),
    ("库存中台",        "supply", "zhaomin",  "华北2集群", "prod",    "online",      "全渠道库存共享", 5, 6),
    ("旧采购系统",      "supply", "liuyang",  "西南灾备集群", "prod", "offline",     "采购 1.0，已下线", 2, 90),
    # 数据平台
    ("实时数仓",        "data",   "sunlei",   "华北2集群", "prod",    "online",      "Flink 实时数仓", 6, 1),
    ("离线调度平台",    "data",   "sunlei",   "华北2集群", "prod",    "maintenance", "离线任务调度", 4, 2),
    ("BI 报表平台",     "data",   "sunlei",   "华东1集群", "prod",    "online",      "经营分析报表", 3, 25),
    ("数据质量中心",    "data",   None,       "华南1集群", "dev",     "developing",  "数据质量规则引擎", 0, 0),
    ("标签画像平台",    "data",   "sunlei",   "华东1集群", "staging", "online",      "用户标签与画像", 5, 4),
    ("日志采集 Agent",  "data",   "sunlei",   "西南灾备集群", "prod", "offline",     "已被 Filebeat 方案替代", 2, 120),
]

ENV_VAR_POOL = [
    ("DB_HOST", "mysql.internal"),
    ("DB_PASSWORD", "****"),
    ("REDIS_URL", "redis://redis.internal:6379/0"),
    ("MQ_BROKER", "kafka://kafka.internal:9092"),
    ("LOG_LEVEL", "INFO"),
    ("OSS_BUCKET", "app-assets"),
]


def seed_if_empty() -> bool:
    """数据库为空时写入初始数据。返回是否执行了种子写入。"""
    if query_one("SELECT id FROM business_lines LIMIT 1"):
        return False

    now = int(time.time())

    bl_ids = {}
    for name, code in BUSINESS_LINES:
        cur = execute("INSERT INTO business_lines (name, code) VALUES (?, ?)", (name, code))
        bl_ids[code] = cur.lastrowid

    user_ids = {}
    for username, name, role, bl_code in USERS:
        cur = execute(
            "INSERT INTO users (username, name, role, business_line_id, token) VALUES (?,?,?,?,?)",
            (username, name, role, bl_ids.get(bl_code), f"tok-{username}-zhiyun"),
        )
        user_ids[username] = cur.lastrowid

    now0 = int(time.time())
    # 业务线×环境 授权（密文权与编辑权分开授予）
    grant_ids: dict[tuple, int] = {}
    for username, bl_code, env, can_edit, can_reveal, granter, days_ago in GRANTS:
        ts = now0 - days_ago * DAY
        cur = execute(
            """INSERT INTO user_grants
               (user_id, business_line_id, environment, can_view_config,
                can_edit_config, can_reveal, granted_by, created_at, updated_at)
               VALUES (?,?,?,1,?,?,?,?,?)""",
            (user_ids[username], bl_ids[bl_code], env,
             1 if can_edit else 0, 1 if can_reveal else 0,
             user_ids[granter], ts, ts),
        )
        grant_ids[(username, bl_code, env)] = cur.lastrowid

    app_ids: dict[str, int] = {}
    for (app_name, bl_code, owner, cluster, env, status, desc,
         env_count, days_ago) in APPS:
        created = now - days_ago * DAY - 3600
        cur = execute(
            """INSERT INTO applications
               (name, business_line_id, owner_id, cluster, environment, status,
                description, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (app_name, bl_ids[bl_code], user_ids.get(owner), cluster, env, status,
             desc, created, now - days_ago * DAY),
        )
        app_id = cur.lastrowid
        app_ids[app_name] = app_id
        for key, value in ENV_VAR_POOL[:env_count]:
            execute("INSERT INTO env_vars (app_id, key, value) VALUES (?,?,?)",
                    (app_id, key, value))
        execute(
            "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
            (app_id, user_ids.get(owner), "创建应用", f"应用「{app_name}」创建，初始状态：在研",
             created),
        )
        # 近 7 天内有变更的应用：补一条状态流转日志，让控制台开箱有数据
        if days_ago <= 6 and status != "developing":
            execute(
                "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
                (app_id, user_ids.get(owner) or user_ids["admin"], "状态变更",
                 f"状态流转至「{STATUS_LABELS[status]}」", now - days_ago * DAY),
            )
        # 每个应用注册四个标准环境（开发/测试/预发/生产），窗口取环境类型默认值
        esvc.ensure_default_environments(app_id, created)

    seed_config_profiles(app_ids, user_ids, now)
    seed_transfers(app_ids, user_ids, bl_ids, now)
    seed_environments_and_health(app_ids, user_ids, now)
    return True


# ---------------------------------------------------------------- 配置档案种子

def _cfg(key, value, value_type="string", scope="global", is_secret=0):
    return {"key": key, "value": value, "value_type": value_type,
            "scope": scope, "is_secret": is_secret}


# 每个应用的配置档案按时间顺序给出多个版本；相邻版本自动产生逐键留痕。
# (应用名, 环境, [(操作人 username, 备注, 距今天数, [配置项...]) ...])
CONFIG_PROFILES = [
    ("支付网关", "prod", [
        ("zhangwei", "支付网关生产环境初始配置", 12, [
            _cfg("DB_HOST", "mysql-pay.prod.internal"),
            _cfg("DB_PORT", "3306", "number"),
            _cfg("DB_PASSWORD", "Pa$$w0rd-Init-2026", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-prod:6379/0"),
            _cfg("REDIS_PASSWORD", "redis-init-secret", is_secret=1),
            _cfg("MQ_BROKER", "kafka://kafka-prod:9092"),
            _cfg("PAY_TIMEOUT_MS", "3000", "number"),
            _cfg("ENABLE_PROFIT_SHARING", "false", "boolean"),
            _cfg("LOG_LEVEL", "INFO"),
        ]),
        ("zhangwei", "缩短支付超时，关闭详细日志", 6, [
            _cfg("DB_HOST", "mysql-pay.prod.internal"),
            _cfg("DB_PORT", "3306", "number"),
            _cfg("DB_PASSWORD", "Pa$$w0rd-Init-2026", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-prod:6379/0"),
            _cfg("REDIS_PASSWORD", "redis-init-secret", is_secret=1),
            _cfg("MQ_BROKER", "kafka://kafka-prod:9092"),
            _cfg("PAY_TIMEOUT_MS", "2000", "number"),
            _cfg("ENABLE_PROFIT_SHARING", "false", "boolean"),
            _cfg("LOG_LEVEL", "WARN"),
        ]),
        ("lina", "开启分账灰度并轮换数据库口令", 1, [
            _cfg("DB_HOST", "mysql-pay.prod.internal"),
            _cfg("DB_PORT", "3306", "number"),
            _cfg("DB_PASSWORD", "Pa$$w0rd-Rot-0918", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-prod:6379/0"),
            _cfg("REDIS_PASSWORD", "redis-init-secret", is_secret=1),
            _cfg("MQ_BROKER", "kafka://kafka-prod-2:9092"),
            _cfg("PAY_TIMEOUT_MS", "2000", "number"),
            _cfg("ENABLE_PROFIT_SHARING", "true", "boolean", "canary"),
            _cfg("LOG_LEVEL", "WARN"),
            _cfg("RATE_LIMIT_QPS", "500", "number"),
        ]),
    ]),
    ("支付网关", "dev", [
        ("zhangwei", "支付网关开发环境配置", 9, [
            _cfg("DB_HOST", "mysql-pay.dev.internal"),
            _cfg("DB_PORT", "3306", "number"),
            _cfg("DB_PASSWORD", "dev-db-password", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-dev:6379/0"),
            _cfg("MQ_BROKER", "kafka://kafka-dev:9092"),
            _cfg("PAY_TIMEOUT_MS", "5000", "number"),
            _cfg("ENABLE_PROFIT_SHARING", "true", "boolean"),
            _cfg("LOG_LEVEL", "DEBUG"),
            _cfg("MOCK_CHANNEL", "true", "boolean"),
        ]),
    ]),
    ("会员中心", "prod", [
        ("wangqiang", "会员中心生产环境初始配置", 8, [
            _cfg("DB_HOST", "mysql-growth.prod.internal"),
            _cfg("DB_PASSWORD", "member-db-secret", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-growth:6379/1"),
            _cfg("POINT_EXPIRE_DAYS", "365", "number"),
            _cfg("LEVEL_RULES", '{"silver":1000,"gold":10000}', "json"),
            _cfg("LOG_LEVEL", "INFO"),
        ]),
        ("chenchen", "积分有效期调整为 730 天", 2, [
            _cfg("DB_HOST", "mysql-growth.prod.internal"),
            _cfg("DB_PASSWORD", "member-db-secret", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-growth:16379/1"),
            _cfg("POINT_EXPIRE_DAYS", "730", "number"),
            _cfg("LEVEL_RULES", '{"silver":1000,"gold":10000,"diamond":50000}', "json"),
            _cfg("LOG_LEVEL", "INFO"),
            _cfg("PUSH_ENABLED", "true", "boolean", "cluster"),
        ]),
    ]),
    ("实时数仓", "prod", [
        ("sunlei", "实时数仓生产配置", 4, [
            _cfg("FLINK_JOBMANAGER", "flink-jm.data.internal:8081"),
            _cfg("CHECKPOINT_INTERVAL_MS", "60000", "number"),
            _cfg("KAFKA_SASL_PASSWORD", "flink-kafka-secret", is_secret=1),
            _cfg("PARALLELISM", "8", "number"),
            _cfg("LOG_LEVEL", "INFO"),
        ]),
    ]),
    # 离线调度平台：经历过负责人交接（周婷 → 孙磊），配置与版本随应用一并移交、连续可溯
    ("离线调度平台", "prod", [
        ("zhouting", "离线调度平台生产初始配置", 30, [
            _cfg("SCHEDULER_DB", "mysql-scheduler.data.internal"),
            _cfg("SCHEDULER_DB_PASSWORD", "sched-init-pwd", is_secret=1),
            _cfg("MAX_PARALLEL_JOBS", "128", "number"),
            _cfg("LOG_LEVEL", "INFO"),
        ]),
        ("sunlei", "接手后轮换调度库口令、提高并发上限", 1, [
            _cfg("SCHEDULER_DB", "mysql-scheduler.data.internal"),
            _cfg("SCHEDULER_DB_PASSWORD", "sched-rot-0918", is_secret=1),
            _cfg("MAX_PARALLEL_JOBS", "192", "number"),
            _cfg("LOG_LEVEL", "INFO"),
            _cfg("ALERT_WEBHOOK", "https://oncall.data.internal/hook"),
        ]),
    ]),
]


def seed_config_profiles(app_ids: dict, user_ids: dict, now: int) -> None:
    """写入配置档案：每个版本一条快照 + 当前值表 + 逐键审计流水。"""
    for app_name, environment, versions in CONFIG_PROFILES:
        app_id = app_ids.get(app_name)
        if app_id is None:
            continue
        prev_map: dict[str, dict] = {}
        for idx, (username, note, days_ago, items) in enumerate(versions, start=1):
            uid = user_ids.get(username) or user_ids["admin"]
            ts = now - days_ago * DAY - 3600
            execute(
                """INSERT INTO config_versions
                   (app_id, environment, version, snapshot, change_note, created_by, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (app_id, environment, idx, json.dumps(items, ensure_ascii=False), note, uid, ts),
            )
            version_id = query_one(
                "SELECT id FROM config_versions WHERE app_id=? AND environment=? AND version=?",
                (app_id, environment, idx),
            )["id"]
            audit = []
            new_map = {it["key"]: it for it in items}
            for key in sorted(set(prev_map) | set(new_map)):
                old, new = prev_map.get(key), new_map.get(key)
                if old is None and new is not None:
                    audit.append(("add", key, None, new["value"], new["is_secret"], ""))
                elif new is None and old is not None:
                    audit.append(("remove", key, old["value"], None, old["is_secret"], ""))
                elif old is not None and new is not None:
                    meta = []
                    if old["value_type"] != new["value_type"]:
                        meta.append(f"类型 {old['value_type']}→{new['value_type']}")
                    if old["scope"] != new["scope"]:
                        meta.append(f"范围 {old['scope']}→{new['scope']}")
                    if old["is_secret"] != new["is_secret"]:
                        meta.append("密文标记变更")
                    audit.append(("update", key, old["value"], new["value"],
                                  old["is_secret"] or new["is_secret"], "；".join(meta)))
            for action, key, old_v, new_v, is_secret, reason in audit:
                execute(
                    """INSERT INTO config_audit_logs
                       (app_id, environment, version_id, user_id, action, config_key,
                        old_value, new_value, is_secret, reason, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (app_id, environment, version_id, uid, action, key,
                     old_v, new_v, is_secret, reason, ts),
                )
            prev_map = new_map

        # 当前值表落到最后一个版本
        latest = items
        latest_ts = ts
        execute("DELETE FROM config_items WHERE app_id = ? AND environment = ?",
                (app_id, environment))
        for it in latest:
            execute(
                """INSERT INTO config_items
                   (app_id, environment, key, value, value_type, scope, is_secret, updated_by, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (app_id, environment, it["key"], it["value"], it["value_type"],
                 it["scope"], it["is_secret"], uid, latest_ts),
            )

    # 一条"查看明文"留痕样例（昨天，李娜排查支付问题时申请查看库口令）
    pay = app_ids.get("支付网关")
    if pay:
        pwd = query_one(
            "SELECT id, key FROM config_items WHERE app_id=? AND environment='prod' AND is_secret=1 AND key='DB_PASSWORD'",
            (pay,),
        )
        if pwd:
            execute(
                """INSERT INTO config_audit_logs
                   (app_id, environment, version_id, user_id, action, config_key,
                    old_value, new_value, is_secret, reason, created_at)
                   VALUES (?,?,NULL,?, 'reveal', ?, NULL, NULL, 1, ?, ?)""",
                (pay, "prod", user_ids["lina"], pwd["key"],
                 "线上支付失败率升高，排查数据库连接鉴权问题，工单 INC-20260918-07",
                 now - DAY),
            )


def seed_environments_and_health(app_ids: dict, user_ids: dict, now: int) -> None:
    """环境发布窗口 + 实例健康演示数据。

    - 支付网关·生产：一个实例昨天掉线（作战台红色告警）；国庆封网；窗口被调整过；
    - 消息推送中心·生产：一个实例近 24h 重启 6 次（频繁重启，红色，区别于正常重启）；
    - 风控实时引擎·生产：一个实例近 7 天重启 8 次但 24h 内仅 1 次（重启偏多，黄色）；
    - 会员中心：新建自定义「灰度」环境（全时段），生产实例健康、仅半个月前重启过一次；
    - 实时数仓：一个实例曾掉线后恢复。
    所有窗口调整 / 封网 / 掉线 / 重启 / 恢复均同步写 ops_audit_logs。
    """
    HOUR = 3600

    def env_id(app_name, key):
        return query_one(
            "SELECT id FROM app_environments WHERE app_id=? AND env_key=?",
            (app_ids[app_name], key),
        )["id"]

    def set_window(app_name, key, restricted, days, start, end):
        eid = env_id(app_name, key)
        execute(
            "UPDATE app_environments SET deploy_restricted=?, window_days=?, window_start=?, window_end=?, updated_at=? WHERE id=?",
            (1 if restricted else 0, json.dumps(days), start, end, now, eid),
        )
        return eid

    def opslog(app_name, env_key, env_label, category, target, detail, actor, ts):
        execute(
            """INSERT INTO ops_audit_logs
               (app_id, environment, environment_label, category, target_name, detail, actor_id, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (app_ids[app_name], env_key, env_label, category, target, detail,
             user_ids.get(actor), ts),
        )

    def holiday(app_name, key, date, reason, actor, days_ago):
        eid = env_id(app_name, key)
        execute(
            "INSERT INTO env_holidays (env_id, app_id, holiday_date, reason, created_by, created_at) VALUES (?,?,?,?,?,?)",
            (eid, app_ids[app_name], date, reason, user_ids[actor], now - days_ago * DAY),
        )

    def instance(app_name, key, name, status, restart_count,
                 last_restart_at, last_seen_at, created_hours_ago):
        eid = env_id(app_name, key)
        aid = app_ids[app_name]
        created = now - created_hours_ago * HOUR
        execute(
            """INSERT INTO app_instances
               (app_id, env_id, environment, name, status, restart_count,
                last_restart_at, last_seen_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (aid, eid, key, name, status, restart_count, last_restart_at,
             last_seen_at, created, last_seen_at),
        )
        return query_one("SELECT id FROM app_instances WHERE app_id=? AND env_id=? AND name=?",
                         (aid, eid, name))["id"]

    def event(app_name, key, inst_id, etype, detail, actor, hours_ago):
        eid = env_id(app_name, key)
        ts = now - hours_ago * HOUR
        execute(
            """INSERT INTO instance_events
               (instance_id, app_id, env_id, environment, event_type, detail, actor_id, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (inst_id, app_ids[app_name], eid, key, etype, detail,
             user_ids.get(actor), ts),
        )
        return ts

    # ---- 发布窗口调整 + 国庆封网（支付网关·生产）----
    set_window("支付网关", "prod", 1, [1, 3], "10:00", "18:00")  # 周二、周四
    opslog("支付网关", "prod", "生产", "window_update", "生产",
           "发布窗口调整：每周 周一、周三、周五 10:00–19:00 → 每周 周二、周四 10:00–18:00",
           "zhangwei", now - 3 * DAY)
    holiday("支付网关", "prod", "2026-10-01", "国庆节封网（10/1–10/7 不发布）", "admin", 5)
    holiday("支付网关", "prod", "2026-10-02", "国庆节封网", "admin", 5)
    opslog("支付网关", "prod", "生产", "holiday_add", "2026-10-01",
           "新增封网日 2026-10-01（国庆节封网（10/1–10/7 不发布），当天关闭发布）",
           "admin", now - 5 * DAY)

    # ---- 支付网关·生产：3 实例，1 掉线 ----
    i1 = instance("支付网关", "prod", "pay-gw-7d9c4-2c8q9", "alive", 1,
                  now - 9 * DAY, now - 2 * HOUR, 24 * 40)
    event("支付网关", "prod", i1, "restart", "版本 v3 滚动发布重启", "zhangwei", 9 * 24)
    i2 = instance("支付网关", "prod", "pay-gw-7d9c4-7k2mz", "alive", 1,
                  now - 9 * DAY, now - 2 * HOUR, 24 * 40)
    event("支付网关", "prod", i2, "restart", "版本 v3 滚动发布重启", "zhangwei", 9 * 24)
    offline_ts = now - 19 * HOUR  # 昨天夜间掉线
    i3 = instance("支付网关", "prod", "pay-gw-7d9c4-x4vbn", "offline", 2,
                  now - 2 * DAY, offline_ts, 24 * 38)
    event("支付网关", "prod", i3, "restart", "节点健康检查失败后自愈重启", None, 2 * 24)
    event("支付网关", "prod", i3, "restart", "版本 v3 滚动发布重启", "zhangwei", 9 * 24)
    event("支付网关", "prod", i3, "offline", "心跳连续 3 次超时（约 15 分钟），监控判定掉线", None, 19)
    opslog("支付网关", "prod", "生产", "instance_offline", "pay-gw-7d9c4-x4vbn",
           "实例 pay-gw-7d9c4-x4vbn 掉线（最后存活 22:40）", None, offline_ts)

    # ---- 消息推送中心·生产：频繁重启（近 24h 6 次，红色）----
    restart_hours = [22, 16, 11, 7, 4, 1]
    mp = instance("消息推送中心", "prod", "msg-push-5b8f1-r8d2k", "alive",
                  len(restart_hours), now - restart_hours[-1] * HOUR, now - 20 * 60,
                  24 * 30)
    for idx, h in enumerate(restart_hours):
        ts = event("消息推送中心", "prod", mp, "restart",
                   "进程 OOM 退出后被拉起（监控记录，非发布）", None, h)
        if idx == len(restart_hours) - 1:
            opslog("消息推送中心", "prod", "生产", "instance_restart",
                   "msg-push-5b8f1-r8d2k",
                   f"实例 msg-push-5b8f1-r8d2k 第 {idx + 1} 次重启（近 24 小时内第 6 次）", None, ts)
    # 同环境另两个健康实例作对照
    mp2 = instance("消息推送中心", "prod", "msg-push-5b8f1-t6n3q", "alive", 1,
                   now - 12 * DAY, now - 3 * 60, 24 * 30)
    event("消息推送中心", "prod", mp2, "restart", "例行补丁重启", "wangqiang", 12 * 24)
    instance("消息推送中心", "prod", "msg-push-5b8f1-w9g7p", "alive", 1,
             now - 12 * DAY, now - 5 * 60, 24 * 30)

    # ---- 风控实时引擎·生产：近 7 天 8 次、24h 内 1 次（重启偏多，黄色）----
    rc_hours = [6 * 24 + 2, 5 * 24, 4 * 24 + 6, 3 * 24, 2 * 24 + 3, 36, 30, 26]
    rc = instance("风控实时引擎", "prod", "risk-rt-9c3a8-h5j6m", "alive",
                  len(rc_hours), now - rc_hours[-1] * HOUR, now - 40 * 60, 24 * 60)
    for h in rc_hours:
        event("风控实时引擎", "prod", rc, "restart", "规则引擎分片重平衡触发重启", None, h)
    instance("风控实时引擎", "prod", "risk-rt-9c3a8-q1z8x", "alive", 0,
             None, now - 30 * 60, 24 * 60)

    # ---- 会员中心：自定义「灰度」环境 ----
    gray_eid = query_one(
        "SELECT id FROM app_environments WHERE app_id=? AND env_key='gray'",
        (app_ids["会员中心"],),
    )
    if not gray_eid:
        execute(
            """INSERT INTO app_environments
               (app_id, env_key, env_label, is_builtin, deploy_restricted,
                window_days, window_start, window_end, created_by, created_at, updated_at)
               VALUES (?,?,?,0,0,'[]','00:00','23:59',?,?,?)""",
            (app_ids["会员中心"], "gray", "灰度", user_ids["wangqiang"], now - 2 * DAY, now - 2 * DAY),
        )
        opslog("会员中心", "gray", "灰度", "env_create", "灰度",
               "新增自定义环境「灰度」（标识 gray），默认全时段允许发布",
               "wangqiang", now - 2 * DAY)
        g1 = instance("会员中心", "gray", "member-gray-2f7d1-v3c5r", "alive", 0,
                      None, now - 10 * 60, 24 * 2)
    # 会员中心·生产：健康实例，最近一次重启在半个月前（正常重启对照）
    mb1 = instance("会员中心", "prod", "member-c-4e6b9-m8k2s", "alive", 1,
                   now - 15 * DAY, now - 5 * 60, 24 * 70)
    event("会员中心", "prod", mb1, "restart", "会员等级规则季度更新发布", "wangqiang", 15 * 24)
    instance("会员中心", "prod", "member-c-4e6b9-n4w7y", "alive", 0,
             None, now - 7 * 60, 24 * 70)
    instance("会员中心", "prod", "member-c-4e6b9-p0d9f", "alive", 0,
             None, now - 9 * 60, 24 * 70)

    # ---- 实时数仓·生产：曾掉线后恢复 ----
    dw1 = instance("实时数仓", "prod", "rt-dw-8a2c5-k6m4t", "alive", 1,
                   now - 4 * DAY, now - 60 * 60, 24 * 25)
    event("实时数仓", "prod", dw1, "offline", "TaskManager 失联", None, 4 * 24 + 2)
    event("实时数仓", "prod", dw1, "recover", "节点拉起，作业从 checkpoint 恢复", "sunlei", 4 * 24)
    event("实时数仓", "prod", dw1, "restart", "恢复后随作业重启", "sunlei", 4 * 24)
    opslog("实时数仓", "prod", "生产", "instance_offline", "rt-dw-8a2c5-k6m4t",
           "实例 rt-dw-8a2c5-k6m4t 掉线", None, now - (4 * 24 + 2) * HOUR)
    opslog("实时数仓", "prod", "生产", "instance_recover", "rt-dw-8a2c5-k6m4t",
           "实例 rt-dw-8a2c5-k6m4t 恢复存活", "sunlei", now - 4 * DAY)
    instance("实时数仓", "prod", "rt-dw-8a2c5-j7b3n", "alive", 0,
             None, now - 2 * 60, 24 * 25)

    # ---- 清结算 / 库存中台：普通健康实例，让大多数环境显示正常 ----
    for app_name, prefix, key in [
        ("清结算中心", "settle", "prod"),
        ("库存中台", "stock", "prod"),
        ("订单履约中心", "fulfill", "prod"),
    ]:
        instance(app_name, key, f"{prefix}-a1b2c-node1", "alive", 0, None, now - 60, 24 * 20)
        instance(app_name, key, f"{prefix}-a1b2c-node2", "alive", 0, None, now - 90, 24 * 20)


def seed_transfers(app_ids: dict, user_ids: dict, bl_ids: dict, now: int) -> None:
    """写入一条应用交接留痕（周婷转岗 → 孙磊接手离线调度平台）与若干权限变更留痕样例。

    交接的是应用归属与配置管理权限：配置项、历史版本、逐键留痕都挂在 app_id 上随应用移交，
    这里只补登记交接事实（app_transfers + change_logs + permission_logs）。
    """
    def permlog(actor, target, action, scope_text, detail, ts):
        execute(
            """INSERT INTO permission_logs
               (actor_id, target_user_id, action, scope_text, detail, created_at)
               VALUES (?,?,?,?,?,?)""",
            (user_ids[actor], user_ids[target], action, scope_text, detail, ts),
        )

    # 授权类留痕样例（与 GRANTS 对应，演示"权限变更本身进留痕"）
    permlog("admin", "qianyi", "grant", "用户增长 · 生产",
            "授予 钱一 在「用户增长 · 生产」的权限：配置查看（脱敏）、密文查看明文（只读观察者不可编辑）",
            now - 3 * DAY)
    permlog("admin", "zhouting", "grant", "数据平台 · 生产",
            "授予 周婷 在「数据平台 · 生产」的权限：配置查看（脱敏）",
            now - 10 * DAY)
    permlog("liuyang", "zhaomin", "grant", "供应链 · 生产",
            "授予 赵敏 在「供应链 · 生产」的权限：配置查看（脱敏）、配置编辑、密文查看明文",
            now - 7 * DAY)

    app_id = app_ids.get("离线调度平台")
    if not app_id:
        return
    ts = now - 2 * DAY
    note = "周婷转岗至数据治理组，离线调度平台及其全部环境配置、历史版本与密文授权责任移交孙磊"
    execute(
        """INSERT INTO app_transfers
           (app_id, old_owner_id, new_owner_id, transfer_by_id, note, created_at)
           VALUES (?,?,?,?,?,?)""",
        (app_id, user_ids["zhouting"], user_ids["sunlei"], user_ids["admin"], note, ts),
    )
    execute(
        "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (app_id, user_ids["admin"], "应用交接",
         "应用交接：负责人 周婷 → 孙磊；交接备注：" + note
         + "；配置项与全部历史版本、留痕随应用一并移交", ts),
    )
    permlog("admin", "sunlei", "transfer", "应用「离线调度平台」",
            "应用交接：负责人 周婷 → 孙磊；交接前负责人：周婷，交接后负责人：孙磊；"
            "配置项随应用一并移交" + ("；交接备注：" + note if note else ""), ts)

