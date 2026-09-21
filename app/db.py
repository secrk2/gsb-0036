"""织云系统 - SQLite 数据层。

生命周期状态机（只能向前流转，下线为终态）：
    在研 developing -> 上线 online -> 维保 maintenance -> 下线 offline(终态)
"""
import os
import sqlite3
import threading

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "zhiyun.db")
)

ENVIRONMENTS = ["dev", "test", "staging", "prod"]
ENV_LABELS = {"dev": "开发", "test": "测试", "staging": "预发", "prod": "生产"}
# 内置环境键（业务线仍可在每个应用下增删自定义环境，如灰度/预演）
BUILTIN_ENV_KEYS = ENVIRONMENTS
# 自定义环境键规则：小写字母开头，仅含小写字母数字 _ -
ENV_KEY_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"

STATUSES = ["developing", "online", "maintenance", "offline"]
STATUS_LABELS = {
    "developing": "在研",
    "online": "上线",
    "maintenance": "维保",
    "offline": "下线",
}
STATUS_ORDER = {s: i for i, s in enumerate(STATUSES)}
TERMINAL_STATUS = "offline"

CLUSTERS = ["华东1集群", "华北2集群", "华南1集群", "西南灾备集群"]

# 角色体系：平台管理员 / 业务线负责人 / 应用负责人 / 只读观察者
ROLES = ["admin", "bl_owner", "app_owner", "viewer"]
ROLE_LABELS = {
    "admin": "平台管理员",
    "bl_owner": "业务线负责人",
    "app_owner": "应用负责人",
    "viewer": "只读观察者",
}
# 授权范围中的"全部环境"哨兵值
ENV_SCOPE_ALL = "*"

# 配置档案（按 应用 + 环境 管理）
CONFIG_TYPES = ["string", "number", "boolean", "json"]
CONFIG_TYPE_LABELS = {"string": "字符串", "number": "数字", "boolean": "布尔", "json": "JSON"}
# 生效范围
CONFIG_SCOPES = ["global", "cluster", "canary"]
CONFIG_SCOPE_LABELS = {"global": "全局", "cluster": "集群", "canary": "灰度"}
# 布尔值归一化后的存储形态
BOOL_TRUE = {"true", "1", "yes", "on", "是", "开"}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS business_lines (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    -- admin 平台管理员 / bl_owner 业务线负责人 / app_owner 应用负责人 / viewer 只读观察者
    role             TEXT NOT NULL DEFAULT 'viewer'
                     CHECK (role IN ('admin', 'bl_owner', 'app_owner', 'viewer')),
    business_line_id INTEGER REFERENCES business_lines(id),  -- 业务线负责人/应用负责人的所属业务线；管理员与观察者可为空
    token            TEXT NOT NULL UNIQUE
);

-- 可见范围授权：按 业务线 × 环境 两级收窄。
-- environment='*' 表示该业务线全部环境；具体环境（dev/test/staging/prod）表示仅该环境。
-- 密文查看权 can_reveal 与 配置编辑权 can_edit 分开授予：能看明文不等于能改。
CREATE TABLE IF NOT EXISTS user_grants (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    business_line_id INTEGER NOT NULL REFERENCES business_lines(id) ON DELETE CASCADE,
    -- '*' 表示该业务线全部环境；也允许应用下自定义环境键（如 gray）
    environment      TEXT NOT NULL DEFAULT '*',
    can_view_config  INTEGER NOT NULL DEFAULT 1 CHECK (can_view_config IN (0,1)),
    can_edit_config  INTEGER NOT NULL DEFAULT 0 CHECK (can_edit_config IN (0,1)),
    can_reveal       INTEGER NOT NULL DEFAULT 0 CHECK (can_reveal IN (0,1)),
    granted_by       INTEGER REFERENCES users(id),
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE (user_id, business_line_id, environment)
);

-- 应用归属交接留痕：交接的是应用归属与配置管理权限，配置项随应用一并移交
CREATE TABLE IF NOT EXISTS app_transfers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id           INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    old_owner_id     INTEGER REFERENCES users(id),
    new_owner_id     INTEGER REFERENCES users(id),
    transfer_by_id   INTEGER REFERENCES users(id),   -- 发起/确认交接的人
    note             TEXT NOT NULL DEFAULT '',
    created_at       INTEGER NOT NULL
);

-- 权限与交接类留痕（授权/收权/角色调整等，不只属于单个应用，独立于 change_logs）
CREATE TABLE IF NOT EXISTS permission_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id         INTEGER REFERENCES users(id),       -- 操作人（谁改的权限）
    target_user_id   INTEGER REFERENCES users(id),       -- 被改权限的账号
    action           TEXT NOT NULL,                       -- grant/revoke/role_change/transfer
    scope_text       TEXT NOT NULL DEFAULT '',            -- 业务线/环境/应用的文字描述
    detail           TEXT NOT NULL DEFAULT '',            -- 具体变化（授了什么、收了什么）
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    business_line_id INTEGER NOT NULL REFERENCES business_lines(id),
    owner_id         INTEGER REFERENCES users(id),
    cluster          TEXT NOT NULL,
    -- 环境可由业务线自定义；写死四枚举的旧约束由启动迁移放宽
    environment      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'developing'
                     CHECK (status IN ('developing','online','maintenance','offline')),
    description      TEXT NOT NULL DEFAULT '',
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE (business_line_id, name)
);

CREATE TABLE IF NOT EXISTS env_vars (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL DEFAULT '',
    UNIQUE (app_id, key)
);

CREATE TABLE IF NOT EXISTS change_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id),
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

-- 配置档案：配置项按 应用 + 环境 管理（键、值、类型、生效范围、是否密文）
CREATE TABLE IF NOT EXISTS config_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    environment TEXT NOT NULL CHECK (environment IN ('dev','test','staging','prod')),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',   -- 密文同样落库（内部系统演示，无外部 KMS），接口默认不回传明文
    value_type  TEXT NOT NULL DEFAULT 'string'
                CHECK (value_type IN ('string','number','boolean','json')),
    scope       TEXT NOT NULL DEFAULT 'global'
                CHECK (scope IN ('global','cluster','canary')),
    is_secret   INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0,1)),
    updated_by  INTEGER REFERENCES users(id),
    updated_at  INTEGER NOT NULL,
    UNIQUE (app_id, environment, key)
);

-- 配置版本：每次保存产生一个全量快照；回滚 = 追加新版本，绝不改写历史版本
CREATE TABLE IF NOT EXISTS config_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    environment TEXT NOT NULL,
    version     INTEGER NOT NULL,          -- 该 应用+环境 内自增
    snapshot    TEXT NOT NULL,             -- JSON 全量快照（密文存明文，仅回滚/版本对比内部使用）
    change_note TEXT NOT NULL DEFAULT '',
    created_by  INTEGER REFERENCES users(id),
    created_at  INTEGER NOT NULL,
    UNIQUE (app_id, environment, version)
);-- 配置留痕：逐键流水（改前/改后/操作人/理由），只追加，不更新不删除
CREATE TABLE IF NOT EXISTS config_audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    environment TEXT NOT NULL,
    version_id  INTEGER REFERENCES config_versions(id) ON DELETE SET NULL,
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,             -- add/update/remove/rollback/reveal
    config_key  TEXT NOT NULL DEFAULT '',
    old_value   TEXT,
    new_value   TEXT,
    is_secret   INTEGER NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',  -- reveal 强制填写；rollback 记录目标版本
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_apps_bl ON applications(business_line_id);
CREATE INDEX IF NOT EXISTS idx_apps_owner ON applications(owner_id);
CREATE INDEX IF NOT EXISTS idx_logs_app ON change_logs(app_id);
CREATE INDEX IF NOT EXISTS idx_logs_time ON change_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_grants_user ON user_grants(user_id);
CREATE INDEX IF NOT EXISTS idx_grants_bl_env ON user_grants(business_line_id, environment);
CREATE INDEX IF NOT EXISTS idx_transfers_app ON app_transfers(app_id);
CREATE INDEX IF NOT EXISTS idx_permlogs_target ON permission_logs(target_user_id);
CREATE INDEX IF NOT EXISTS idx_permlogs_time ON permission_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_cfg_app_env ON config_items(app_id, environment);
CREATE INDEX IF NOT EXISTS idx_ver_app_env ON config_versions(app_id, environment);
CREATE INDEX IF NOT EXISTS idx_audit_app ON config_audit_logs(app_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON config_audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON config_audit_logs(action);

-- ====================================================================
-- 环境管理（每应用自定义环境 + 发布窗口）
-- ====================================================================

-- 每个应用下的环境注册表：开发/预发/生产不是写死的，业务线可以自己增删
CREATE TABLE IF NOT EXISTS app_environments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id            INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    env_key           TEXT NOT NULL,             -- dev/test/staging/prod 或自定义键
    env_label         TEXT NOT NULL,             -- 展示名（开发/预发/生产/灰度…）
    is_builtin        INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0,1)),
    -- 发布窗口：deploy_restricted=0 表示全时段允许；=1 时按下面的周几+时段收口
    deploy_restricted INTEGER NOT NULL DEFAULT 0 CHECK (deploy_restricted IN (0,1)),
    window_days       TEXT NOT NULL DEFAULT '[]', -- JSON：允许发布的星期，0=周一 … 6=周日
    window_start      TEXT NOT NULL DEFAULT '09:00', -- HH:MM（本地时区）
    window_end        TEXT NOT NULL DEFAULT '18:00',
    created_by        INTEGER REFERENCES users(id),
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    UNIQUE (app_id, env_key)
);

-- 节假日封网：窗口里的星期规则在这些日期单独关闭
CREATE TABLE IF NOT EXISTS env_holidays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    env_id      INTEGER NOT NULL REFERENCES app_environments(id) ON DELETE CASCADE,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    holiday_date TEXT NOT NULL,                  -- YYYY-MM-DD
    reason      TEXT NOT NULL DEFAULT '',
    created_by  INTEGER REFERENCES users(id),
    created_at  INTEGER NOT NULL,
    UNIQUE (env_id, holiday_date)
);

-- ====================================================================
-- 应用健康（每环境实例）
-- ====================================================================

CREATE TABLE IF NOT EXISTS app_instances (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id           INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    env_id           INTEGER NOT NULL REFERENCES app_environments(id) ON DELETE CASCADE,
    environment      TEXT NOT NULL,             -- 冗余 env_key，便于列表/聚合查询
    name             TEXT NOT NULL,             -- 实例名（pod-xx / host:port）
    status           TEXT NOT NULL DEFAULT 'alive'
                     CHECK (status IN ('alive','offline')),
    restart_count    INTEGER NOT NULL DEFAULT 0,
    last_restart_at  INTEGER,
    last_seen_at     INTEGER NOT NULL,          -- 最近一次存活上报
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE (app_id, env_id, name)
);

-- 实例事件流（重启 / 掉线 / 恢复）：支撑"反复重启 vs 正常重启"与按时间窗追溯
CREATE TABLE IF NOT EXISTS instance_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES app_instances(id) ON DELETE CASCADE,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    env_id      INTEGER NOT NULL REFERENCES app_environments(id) ON DELETE CASCADE,
    environment TEXT NOT NULL,
    event_type  TEXT NOT NULL CHECK (event_type IN ('restart','offline','recover')),
    detail      TEXT NOT NULL DEFAULT '',
    actor_id    INTEGER REFERENCES users(id),   -- NULL = 系统/监控自动
    created_at  INTEGER NOT NULL
);

-- 环境与健康变更留痕（窗口改动 / 节假日封网 / 实例掉线等，独立于配置留痕）
CREATE TABLE IF NOT EXISTS ops_audit_logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id            INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    environment       TEXT NOT NULL DEFAULT '',  -- env_key（环境删除后仍可按键展示）
    environment_label TEXT NOT NULL DEFAULT '',
    category          TEXT NOT NULL,             -- env_create/env_delete/window_update/
                                                 -- holiday_add/holiday_remove/
                                                 -- instance_restart/instance_offline/instance_recover
    target_name       TEXT NOT NULL DEFAULT '',  -- 涉及对象名（实例名/节假日日期等）
    detail            TEXT NOT NULL DEFAULT '',
    actor_id          INTEGER REFERENCES users(id),  -- NULL = 系统
    created_at        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_appenv_app ON app_environments(app_id);
CREATE INDEX IF NOT EXISTS idx_holiday_env ON env_holidays(env_id);
CREATE INDEX IF NOT EXISTS idx_inst_app_env ON app_instances(app_id, environment);
CREATE INDEX IF NOT EXISTS idx_inst_status ON app_instances(status);
CREATE INDEX IF NOT EXISTS idx_iev_inst ON instance_events(instance_id);
CREATE INDEX IF NOT EXISTS idx_iev_app_time ON instance_events(app_id, created_at);
CREATE INDEX IF NOT EXISTS idx_opsaudit_app ON ops_audit_logs(app_id);
CREATE INDEX IF NOT EXISTS idx_opsaudit_time ON ops_audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_opsaudit_cat ON ops_audit_logs(category);
"""

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate_legacy(conn)
    _migrate_env_checks(conn)
    _backfill_app_environments(conn)
    _repair_audit_environment(conn)
    conn.commit()


# 标准环境的默认发布窗口（新建应用 / 旧库回填时使用；业务线随后可自行调整）
# window_days: 0=周一 … 6=周日
DEFAULT_WINDOWS = {
    "dev":     {"restricted": 0, "days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "23:59"},
    "test":    {"restricted": 0, "days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "23:59"},
    "staging": {"restricted": 1, "days": [0, 1, 2, 3, 4],       "start": "10:00", "end": "20:00"},
    "prod":    {"restricted": 1, "days": [1, 3],                 "start": "10:00", "end": "18:00"},
}


def _backfill_app_environments(conn) -> None:
    """确保每个应用都注册了四个标准环境（幂等）。

    环境从"写死枚举"改为"每应用注册表"后，旧库应用需要补齐注册行，
    否则环境维度的配置/实例数据会失去归属。自定义环境不自动补。
    """
    now = int(__import__("time").time())
    apps = conn.execute("SELECT id, created_at FROM applications").fetchall()
    for app in apps:
        ts = app["created_at"] or now
        for key in ENVIRONMENTS:
            exists = conn.execute(
                "SELECT 1 FROM app_environments WHERE app_id=? AND env_key=?",
                (app["id"], key),
            ).fetchone()
            if exists:
                continue
            w = DEFAULT_WINDOWS[key]
            conn.execute(
                """INSERT INTO app_environments
                   (app_id, env_key, env_label, is_builtin, deploy_restricted,
                    window_days, window_start, window_end, created_by, created_at, updated_at)
                   VALUES (?,?,?,1,?,?,?,?,NULL,?,?)""",
                (app["id"], key, ENV_LABELS[key], w["restricted"],
                 json_dumps(w["days"]), w["start"], w["end"], ts, now),
            )


def json_dumps(value) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


def _migrate_env_checks(conn) -> None:
    """放宽旧库写死的环境枚举 CHECK，使自定义环境键可以落库。

    CREATE TABLE IF NOT EXISTS 不会更新既有表约束，SQLite 也不支持 DROP CONSTRAINT，
    这里按官方"重命名 → 建新表 → 搬数据 → 删旧表"流程重建三张表；列定义顺序保持一致，
    数据用 INSERT SELECT 原样搬迁，索引随后重建。
    """
    def table_sql(name: str) -> str:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        # 归一化空白，避免列名与类型间空格数不同导致漏检
        return " ".join(((row["sql"] if row else "") or "").split())

    needs_user_grants = "CHECK (environment IN ('*'" in table_sql("user_grants")
    needs_applications = "environment TEXT NOT NULL CHECK (environment IN ('dev'" in table_sql("applications")
    needs_config_items = (
        "config_items" in table_sql("config_items")
        and "environment TEXT NOT NULL CHECK (environment IN ('dev'" in table_sql("config_items")
    )
    if not (needs_user_grants or needs_applications or needs_config_items):
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        if needs_user_grants:
            conn.execute("ALTER TABLE user_grants RENAME TO user_grants_old")
            conn.executescript("""
                CREATE TABLE user_grants (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    business_line_id INTEGER NOT NULL REFERENCES business_lines(id) ON DELETE CASCADE,
                    environment      TEXT NOT NULL DEFAULT '*',
                    can_view_config  INTEGER NOT NULL DEFAULT 1 CHECK (can_view_config IN (0,1)),
                    can_edit_config  INTEGER NOT NULL DEFAULT 0 CHECK (can_edit_config IN (0,1)),
                    can_reveal       INTEGER NOT NULL DEFAULT 0 CHECK (can_reveal IN (0,1)),
                    granted_by       INTEGER REFERENCES users(id),
                    created_at       INTEGER NOT NULL,
                    updated_at       INTEGER NOT NULL,
                    UNIQUE (user_id, business_line_id, environment)
                );""")
            conn.execute("INSERT INTO user_grants SELECT * FROM user_grants_old")
            conn.execute("DROP TABLE user_grants_old")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grants_user ON user_grants(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grants_bl_env ON user_grants(business_line_id, environment)")

        if needs_applications:
            conn.execute("ALTER TABLE applications RENAME TO applications_old")
            conn.executescript("""
                CREATE TABLE applications (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    name             TEXT NOT NULL,
                    business_line_id INTEGER NOT NULL REFERENCES business_lines(id),
                    owner_id         INTEGER REFERENCES users(id),
                    cluster          TEXT NOT NULL,
                    environment      TEXT NOT NULL,
                    status           TEXT NOT NULL DEFAULT 'developing'
                                     CHECK (status IN ('developing','online','maintenance','offline')),
                    description      TEXT NOT NULL DEFAULT '',
                    created_at       INTEGER NOT NULL,
                    updated_at       INTEGER NOT NULL,
                    UNIQUE (business_line_id, name)
                );""")
            conn.execute("INSERT INTO applications SELECT * FROM applications_old")
            conn.execute("DROP TABLE applications_old")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_apps_bl ON applications(business_line_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_apps_owner ON applications(owner_id)")

        if needs_config_items:
            conn.execute("ALTER TABLE config_items RENAME TO config_items_old")
            conn.executescript("""
                CREATE TABLE config_items (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                    environment TEXT NOT NULL,
                    key         TEXT NOT NULL,
                    value       TEXT NOT NULL DEFAULT '',
                    value_type  TEXT NOT NULL DEFAULT 'string'
                                CHECK (value_type IN ('string','number','boolean','json')),
                    scope       TEXT NOT NULL DEFAULT 'global'
                                CHECK (scope IN ('global','cluster','canary')),
                    is_secret   INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0,1)),
                    updated_by  INTEGER REFERENCES users(id),
                    updated_at  INTEGER NOT NULL,
                    UNIQUE (app_id, environment, key)
                );""")
            conn.execute("INSERT INTO config_items SELECT * FROM config_items_old")
            conn.execute("DROP TABLE config_items_old")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cfg_app_env ON config_items(app_id, environment)")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise RuntimeError(f"环境枚举迁移后发现悬挂外键：{tuple(bad)[:3]}")


def _repair_audit_environment(conn) -> None:
    """修复历史脏数据：逐键改动流水必须与所属版本的环境一致。

    旧版保存逻辑在出现非 global（集群/灰度）键时，会把整批流水错挂到
    “应用所属环境”，导致预发等环境的改动串进生产留痕并绕过按环境的可见范围。
    这里以 config_versions.environment 为权威来源回填纠正；
    reveal 流水 version_id 为 NULL 且本就按实际环境记录，不在修复范围内。
    """
    conn.execute(
        """UPDATE config_audit_logs
           SET environment = (
               SELECT v.environment FROM config_versions v
               WHERE v.id = config_audit_logs.version_id)
           WHERE version_id IS NOT NULL
             AND action IN ('add','update','remove','rollback')
             AND environment <> (
               SELECT v.environment FROM config_versions v
               WHERE v.id = config_audit_logs.version_id)"""
    )


def _migrate_legacy(conn) -> None:
    """旧版库（users.role 仅 admin/member）平滑升级到四角色体系。

    SQL 的 CREATE TABLE IF NOT EXISTS 不会更新既有表约束，这里检测到旧表后
    手工重建 users，并为旧成员补一条"整条业务线全权"授权，保持迁移前能力。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not row or "'member'" not in (row["sql"] or ""):
        return
    now = int(__import__("time").time())
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE users RENAME TO users_legacy")
    conn.execute(
        """CREATE TABLE users (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            username         TEXT NOT NULL UNIQUE,
            name             TEXT NOT NULL,
            role             TEXT NOT NULL DEFAULT 'viewer'
                             CHECK (role IN ('admin', 'bl_owner', 'app_owner', 'viewer')),
            business_line_id INTEGER REFERENCES business_lines(id),
            token            TEXT NOT NULL UNIQUE
        )"""
    )
    conn.execute(
        """INSERT INTO users (id, username, name, role, business_line_id, token)
           SELECT id, username, name,
                  CASE role WHEN 'admin' THEN 'admin' ELSE 'app_owner' END,
                  business_line_id, token
           FROM users_legacy"""
    )
    conn.execute(
        """INSERT INTO user_grants
               (user_id, business_line_id, environment,
                can_view_config, can_edit_config, can_reveal, granted_by, created_at, updated_at)
           SELECT id, business_line_id, '*', 1, 1, 1, id, ?, ?
           FROM users_legacy WHERE role <> 'admin' AND business_line_id IS NOT NULL""",
        (now, now),
    )
    conn.execute("DROP TABLE users_legacy")
    conn.execute("PRAGMA foreign_keys=ON")



def query(sql: str, params: tuple = ()) -> list:
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()):
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    cur = get_conn().execute(sql, params)
    get_conn().commit()
    return cur
