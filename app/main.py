"""织云系统 - 应用台账 & 资产控制台 API。"""
import os
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import permissions as perms
from . import env_service as esvc
from .auth import (
    User, ensure_bl_visible, err, get_app_checked, get_app_or_404,
    get_app_writable, is_admin, public_user,
)
from .db import (
    CLUSTERS, ENV_LABELS, ENVIRONMENTS, ROLES, ROLE_LABELS, STATUS_LABELS, STATUS_ORDER,
    STATUSES, TERMINAL_STATUS, execute, get_conn, init_db, query, query_one,
)
from .routers import admin as admin_router
from .routers import config as config_router
from .routers import ops as ops_router
from .seed import seed_if_empty

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="织云系统", docs_url=None, redoc_url=None)
app.include_router(config_router.router)
app.include_router(admin_router.router)
app.include_router(ops_router.router)


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_if_empty()
    # 预热审计索引（config_audit_logs 没有 scope 列，按实际存在的索引列预热）
    query("SELECT created_at FROM config_audit_logs LIMIT 1")


# ---------------------------------------------------------------- 基础工具

def log_change(app_id: int, user_id: int, action: str, detail: str) -> None:
    execute(
        "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (app_id, user_id, action, detail, int(time.time())),
    )


def touch(app_id: int) -> None:
    execute("UPDATE applications SET updated_at = ? WHERE id = ?", (int(time.time()), app_id))


def app_to_dict(row, with_env: bool = False) -> dict:
    app_id = row["id"]
    owner = query_one("SELECT id, name FROM users WHERE id = ?", (row["owner_id"],)) if row["owner_id"] else None
    bl = query_one("SELECT id, name FROM business_lines WHERE id = ?", (row["business_line_id"],))
    env_vars = query("SELECT key, value FROM env_vars WHERE app_id = ? ORDER BY key", (app_id,))
    missing_owner = row["owner_id"] is None
    missing_env = len(env_vars) == 0
    data = {
        "id": app_id,
        "name": row["name"],
        "business_line_id": row["business_line_id"],
        "business_line_name": bl["name"] if bl else "",
        "owner_id": row["owner_id"],
        "owner_name": owner["name"] if owner else None,
        "cluster": row["cluster"],
        "environment": row["environment"],
        "environment_label": ENV_LABELS.get(row["environment"], row["environment"]),
        "status": row["status"],
        "status_label": STATUS_LABELS[row["status"]],
        "description": row["description"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "env_var_count": len(env_vars),
        "red_dots": ([{"type": "missing_owner", "label": "缺失负责人"}] if missing_owner else [])
                    + ([{"type": "missing_env", "label": "环境变量缺失"}] if missing_env else []),
    }
    if with_env:
        data["env_vars"] = [dict(v) for v in env_vars]
    return data


# ---------------------------------------------------------------- 请求模型

class LoginIn(BaseModel):
    username: str


class AppCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    business_line_id: int
    owner_id: int | None = None
    cluster: str
    environment: str
    description: str = ""


class AppUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    owner_id: int | None = None
    set_owner: bool = False          # 显式区分"不修改"与"清空负责人"
    cluster: str | None = None
    environment: str | None = None
    description: str | None = None


class StatusIn(BaseModel):
    status: str


class EnvVarsIn(BaseModel):
    vars: list[dict]


# ---------------------------------------------------------------- 认证

@app.post("/api/login")
def login(body: LoginIn):
    row = query_one(
        """SELECT u.id, u.username, u.name, u.role, u.token, u.business_line_id,
                  b.name AS business_line_name
           FROM users u LEFT JOIN business_lines b ON b.id = u.business_line_id
           WHERE u.username = ?""",
        (body.username.strip(),),
    )
    if not row:
        raise err(401, "用户不存在")
    token = row["token"]
    user = perms.build_principal(dict(row))
    return {"token": token, "user": public_user(user)}


@app.get("/api/public/users")
def public_users():
    """登录页可选账号列表（内部系统演示，不暴露令牌）。"""
    rows = query(
        """SELECT u.id, u.username, u.name, u.role, b.name AS business_line_name
           FROM users u LEFT JOIN business_lines b ON b.id = u.business_line_id
           ORDER BY CASE u.role WHEN 'admin' THEN 0 WHEN 'bl_owner' THEN 1
                                WHEN 'app_owner' THEN 2 ELSE 3 END, u.id"""
    )
    return [{
        "id": r["id"], "username": r["username"], "name": r["name"],
        "role": r["role"], "role_label": ROLE_LABELS.get(r["role"], r["role"]),
        "business_line_name": r["business_line_name"],
    } for r in rows]


@app.get("/api/me")
def me(user: dict = User):
    return public_user(user)


# ---------------------------------------------------------------- 元数据

@app.get("/api/meta")
def meta(user: dict = User):
    return {
        "environments": [{"value": e, "label": ENV_LABELS[e]} for e in ENVIRONMENTS],
        "statuses": [{"value": s, "label": STATUS_LABELS[s]} for s in STATUSES],
        "clusters": CLUSTERS,
        "roles": [{"value": r, "label": ROLE_LABELS[r]} for r in ROLES],
    }


@app.get("/api/environments/catalog")
def environments_catalog(user: dict = User, business_line_id: int | None = None):
    """当前可见范围内出现过的全部环境（内置 + 各应用自定义），用于筛选器与标签。"""
    sql = """SELECT DISTINCT e.env_key, e.env_label, e.is_builtin,
                    a.business_line_id, b.name AS business_line_name
             FROM app_environments e
             JOIN applications a ON a.id = e.app_id
             JOIN business_lines b ON b.id = a.business_line_id
             WHERE 1=1"""
    params: list = []
    if is_admin(user):
        if business_line_id:
            sql += " AND a.business_line_id=?"
            params.append(business_line_id)
    else:
        cond, cp = perms.scope_condition(user, "a.business_line_id", "e.env_key", "a.owner_id")
        sql += f" AND {cond}"
        params.extend(cp)
        if business_line_id:
            ensure_bl_visible(user, business_line_id)
            sql += " AND a.business_line_id=?"
            params.append(business_line_id)
    rows = query(sql, tuple(params))
    order = {k: i for i, k in enumerate(ENVIRONMENTS)}
    merged: dict[str, dict] = {}
    for r in rows:
        key = r["env_key"]
        label = ENV_LABELS.get(key, r["env_label"])
        if key not in merged:
            merged[key] = {"value": key, "label": label, "is_builtin": bool(r["is_builtin"])}
    return sorted(merged.values(), key=lambda x: (order.get(x["value"], 99), x["value"]))


@app.get("/api/business-lines")
def business_lines(user: dict = User):
    if is_admin(user):
        rows = query("SELECT id, name, code FROM business_lines ORDER BY id")
    else:
        visible = perms.visible_business_lines(user)
        if not visible:
            return []
        placeholders = ",".join("?" * len(visible))
        rows = query(
            f"SELECT id, name, code FROM business_lines WHERE id IN ({placeholders}) ORDER BY id",
            tuple(visible),
        )
    return [dict(r) for r in rows]


@app.get("/api/users")
def users(user: dict = User, business_line_id: int | None = None):
    """人员目录：管理员全部；业务线负责人本业务线；其余角色限其可见业务线内成员。"""
    sql = """SELECT u.id, u.name, u.username, u.role, u.business_line_id,
                    b.name AS business_line_name
             FROM users u LEFT JOIN business_lines b ON b.id = u.business_line_id"""
    params: list = []
    if is_admin(user):
        if business_line_id:
            sql += " WHERE u.business_line_id = ?"
            params.append(business_line_id)
    else:
        visible = perms.visible_business_lines(user)
        if business_line_id:
            ensure_bl_visible(user, business_line_id)
            ids = [business_line_id]
        else:
            ids = sorted(visible)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        sql += f" WHERE u.business_line_id IN ({placeholders})"
        params.extend(ids)
    sql += " ORDER BY u.id"
    return [dict(r) for r in query(sql, tuple(params))]


# ---------------------------------------------------------------- 应用台账

@app.get("/api/apps")
def list_apps(user: dict = User,
              business_line_id: int | None = None,
              owner_id: int | None = None,
              environment: str | None = None,
              status: str | None = None,
              q: str | None = None):
    sql = "SELECT * FROM applications WHERE 1=1"
    params: list = []
    if is_admin(user):
        if business_line_id:
            sql += " AND business_line_id = ?"
            params.append(business_line_id)
    else:
        # 显式按范围外业务线筛选属于越权：403 并说明可访问范围，而非静默空列表
        if business_line_id:
            ensure_bl_visible(user, business_line_id)
        # 可见范围：本人负责的应用 + 被授权的 业务线×环境 + 业务线负责人的整条业务线
        cond, cond_params = perms.scope_condition(user, "business_line_id", "environment", "owner_id")
        sql += f" AND {cond}"
        params.extend(cond_params)
    if owner_id:
        sql += " AND owner_id = ?"
        params.append(owner_id)
    if environment:
        # 环境不再写死：内置四环境与各应用自定义环境键都可筛选
        if not __import__("re").fullmatch(r"[a-z0-9_-]{1,32}", environment):
            raise err(400, f"非法环境：{environment}")
        sql += " AND environment = ?"
        params.append(environment)
    if status:
        if status not in STATUSES:
            raise err(400, f"非法状态：{status}，可选：{'/'.join(STATUSES)}")
        sql += " AND status = ?"
        params.append(status)
    if q:
        sql += " AND name LIKE ?"
        params.append(f"%{q.strip()}%")
    sql += " ORDER BY updated_at DESC, id DESC"
    return [app_to_dict(r) for r in query(sql, tuple(params))]


@app.post("/api/apps", status_code=201)
def create_app(body: AppCreateIn, user: dict = User):
    # 新建应用属于业务线管理动作：平台管理员或该业务线负责人
    perms.ensure_can_manage_bl(user, body.business_line_id)
    if body.environment not in ENVIRONMENTS:
        raise err(400, f"非法环境：{body.environment}")
    if body.cluster not in CLUSTERS:
        raise err(400, f"非法集群：{body.cluster}，可选：{'、'.join(CLUSTERS)}")
    if body.owner_id is not None:
        owner = query_one("SELECT id, business_line_id, role FROM users WHERE id = ?", (body.owner_id,))
        if not owner:
            raise err(400, "负责人不存在")
        if owner["business_line_id"] != body.business_line_id:
            raise err(400, "负责人必须属于应用所在业务线")
        if owner["role"] == "viewer":
            raise err(400, "只读观察者不能担任应用负责人；如需指定，请先由平台管理员调整其角色")
    dup = query_one("SELECT id FROM applications WHERE business_line_id = ? AND name = ?",
                    (body.business_line_id, body.name.strip()))
    if dup:
        raise err(409, f"同一业务线下应用名不能重复：「{body.name.strip()}」已存在（应用 #{dup['id']}）")
    now = int(time.time())
    cur = execute(
        """INSERT INTO applications
           (name, business_line_id, owner_id, cluster, environment, status,
            description, created_at, updated_at)
           VALUES (?,?,?,?,?,'developing',?,?,?)""",
        (body.name.strip(), body.business_line_id, body.owner_id, body.cluster,
         body.environment, body.description.strip(), now, now),
    )
    log_change(cur.lastrowid, user["id"], "创建应用", f"应用「{body.name.strip()}」创建，初始状态：在研")
    # 环境改为每应用注册表：新建即开通四个标准环境（窗口默认值见 DEFAULT_WINDOWS）
    esvc.ensure_default_environments(cur.lastrowid, now)
    return app_to_dict(get_app_or_404(cur.lastrowid), with_env=True)


@app.get("/api/apps/{app_id}")
def app_detail(app_id: int, user: dict = User):
    app_row = get_app_checked(user, app_id)
    data = app_to_dict(app_row, with_env=True)
    data["can_manage"] = perms.can_manage_app(user, app_row)
    data["can_transfer"] = (
        perms.can_manage_bl(user, app_row["business_line_id"])
        or (app_row["owner_id"] == user["id"] and user["role"] != "viewer")
    )
    logs = query(
        """SELECT l.action, l.detail, l.created_at, u.name AS user_name
           FROM change_logs l LEFT JOIN users u ON u.id = l.user_id
           WHERE l.app_id = ? ORDER BY l.created_at DESC, l.id DESC LIMIT 50""",
        (app_id,),
    )
    data["change_logs"] = [dict(r) for r in logs]
    transfers = query(
        """SELECT t.id, t.old_owner_id, t.new_owner_id, t.note, t.created_at,
                  ou.name AS old_owner_name, nu.name AS new_owner_name,
                  du.name AS transfer_by_name
           FROM app_transfers t
           LEFT JOIN users ou ON ou.id = t.old_owner_id
           LEFT JOIN users nu ON nu.id = t.new_owner_id
           LEFT JOIN users du ON du.id = t.transfer_by_id
           WHERE t.app_id = ? ORDER BY t.created_at DESC, t.id DESC""",
        (app_id,),
    )
    data["transfers"] = [dict(r) for r in transfers]
    return data


@app.patch("/api/apps/{app_id}")
def update_app(app_id: int, body: AppUpdateIn, user: dict = User):
    app_row = get_app_writable(user, app_id)
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），所有信息只读，禁止修改")
    changes = []
    if body.name is not None and body.name.strip() != app_row["name"]:
        dup = query_one("SELECT id FROM applications WHERE business_line_id = ? AND name = ? AND id != ?",
                        (app_row["business_line_id"], body.name.strip(), app_id))
        if dup:
            raise err(409, f"同一业务线下应用名不能重复：「{body.name.strip()}」已存在（应用 #{dup['id']}）")
        changes.append(("name", body.name.strip(), f"应用更名：{app_row['name']} → {body.name.strip()}"))
    if body.set_owner:
        new_owner = body.owner_id
        if new_owner != app_row["owner_id"]:
            # 应用归属变更 = 交接，必须走 /api/apps/{id}/transfer 并留痕，
            # 不允许在普通信息编辑里静默换负责人。
            raise err(
                400,
                "应用归属（负责人）变更属于交接，必须通过「应用交接」流程办理，"
                "系统会记录交接前后负责人与时间；普通信息编辑不接受直接改负责人。",
            )
    if body.cluster is not None and body.cluster != app_row["cluster"]:
        if body.cluster not in CLUSTERS:
            raise err(400, f"非法集群：{body.cluster}")
        changes.append(("cluster", body.cluster, f"集群变更：{app_row['cluster']} → {body.cluster}"))
    if body.environment is not None and body.environment != app_row["environment"]:
        # 主环境必须是该应用下已注册的环境（四个标准环境或业务线自建环境）
        env_row = query_one(
            "SELECT env_key, env_label FROM app_environments WHERE app_id=? AND env_key=?",
            (app_id, body.environment),
        )
        if not env_row:
            raise err(400, f"该应用下不存在环境「{body.environment}」，请先在环境管理中新增")
        changes.append(("environment", body.environment,
                        f"环境变更：{ENV_LABELS.get(app_row['environment'], app_row['environment'])} → {env_row['env_label']}"))
    if body.description is not None and body.description.strip() != app_row["description"]:
        changes.append(("description", body.description.strip(), "更新应用描述"))
    for field, value, _log in changes:
        execute(f"UPDATE applications SET {field} = ? WHERE id = ?", (value, app_id))
    for _field, _value, log_text in changes:
        log_change(app_id, user["id"], "信息变更", log_text)
    if changes:
        touch(app_id)
    return app_to_dict(get_app_or_404(app_id), with_env=True)


@app.post("/api/apps/{app_id}/status")
def change_status(app_id: int, body: StatusIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "生命周期状态")
    old, new = app_row["status"], body.status
    if new not in STATUSES:
        raise err(400, f"非法状态：{new}，可选：{'/'.join(STATUSES)}")
    if old == TERMINAL_STATUS:
        raise err(400, "应用已下线，「下线」为生命周期终态，不能再做任何状态变更")
    if new == old:
        raise err(400, f"应用已处于「{STATUS_LABELS[old]}」状态，无需变更")
    if STATUS_ORDER[new] < STATUS_ORDER[old]:
        raise err(400,
                  f"非法状态回退：不允许从「{STATUS_LABELS[old]}」回退到「{STATUS_LABELS[new]}」。"
                  f"生命周期只能向前流转：在研 → 上线 → 维保 → 下线")
    execute("UPDATE applications SET status = ? WHERE id = ?", (new, app_id))
    touch(app_id)
    log_change(app_id, user["id"], "状态变更",
               f"{STATUS_LABELS[old]} → {STATUS_LABELS[new]}")
    return app_to_dict(get_app_or_404(app_id), with_env=True)


@app.put("/api/apps/{app_id}/env-vars")
def put_env_vars(app_id: int, body: EnvVarsIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "环境变量")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），环境变量只读，禁止修改")
    seen: set = set()
    cleaned = []
    for item in body.vars:
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        if key in seen:
            raise err(400, f"环境变量 key 重复：{key}")
        seen.add(key)
        cleaned.append((key, str(item.get("value", ""))))
    conn = get_conn()
    conn.execute("DELETE FROM env_vars WHERE app_id = ?", (app_id,))
    conn.executemany("INSERT INTO env_vars (app_id, key, value) VALUES (?,?,?)",
                     [(app_id, k, v) for k, v in cleaned])
    conn.commit()
    touch(app_id)
    log_change(app_id, user["id"], "环境变量变更", f"环境变量更新为 {len(cleaned)} 项")
    return app_to_dict(get_app_or_404(app_id), with_env=True)


# ---------------------------------------------------------------- 资产控制台

@app.get("/api/console/summary")
def console_summary(user: dict = User):
    if is_admin(user):
        scope_sql, scope_params = "1=1", []
    else:
        scope_sql, scope_params = perms.scope_condition(
            user, "a.business_line_id", "a.environment", "a.owner_id"
        )

    bl_stats_sql = """SELECT b.id, b.name,
                   COUNT(a.id) AS total,
                   SUM(CASE WHEN a.status = 'developing'   THEN 1 ELSE 0 END) AS developing,
                   SUM(CASE WHEN a.status = 'online'       THEN 1 ELSE 0 END) AS online,
                   SUM(CASE WHEN a.status = 'maintenance'  THEN 1 ELSE 0 END) AS maintenance,
                   SUM(CASE WHEN a.status = 'offline'      THEN 1 ELSE 0 END) AS offline
            FROM business_lines b
            LEFT JOIN applications a ON a.business_line_id = b.id AND {scope}
            {where}
            GROUP BY b.id HAVING total > 0 OR {bl_in_scope}
            ORDER BY total DESC, b.id"""
    if is_admin(user):
        by_bl = query(bl_stats_sql.format(scope="1=1", where="", bl_in_scope="1=1"))
    else:
        visible_bls = sorted(perms.visible_business_lines(user))
        bl_ph = ",".join("?" * len(visible_bls)) if visible_bls else "0"
        # LEFT JOIN 的范围条件必须放在 ON 上；WHERE 只保留"有可见业务线"的行
        by_bl = query(
            bl_stats_sql.format(scope=scope_sql, where=f"WHERE b.id IN ({bl_ph})",
                                bl_in_scope="1=1"),
            (*scope_params, *visible_bls),
        )

    week_ago = int(time.time()) - 7 * 86400
    recent = query(
        f"""SELECT a.id, a.name, b.name AS business_line_name, a.status,
                   MAX(l.created_at) AS last_changed_at, COUNT(l.id) AS change_count
            FROM change_logs l
            JOIN applications a ON a.id = l.app_id
            JOIN business_lines b ON b.id = a.business_line_id
            WHERE l.created_at >= ? AND {scope_sql}
            GROUP BY a.id ORDER BY last_changed_at DESC LIMIT 20""",
        (week_ago, *scope_params),
    )

    apps = query(
        f"SELECT a.* FROM applications a WHERE {scope_sql}",
        tuple(scope_params),
    )
    missing_owner, missing_env = [], []
    for row in apps:
        if row["owner_id"] is None:
            missing_owner.append(app_to_dict(row))
        if not query_one("SELECT id FROM env_vars WHERE app_id = ? LIMIT 1", (row["id"],)):
            missing_env.append(app_to_dict(row))

    return {
        "by_business_line": [dict(r) for r in by_bl],
        "recent_changed_apps": [dict(r) for r in recent],
        "health_alerts": esvc.console_alerts(user),
        "red_dots": {
            "missing_owner": missing_owner,
            "missing_env": missing_env,
        },
        "totals": {
            "apps": len(apps),
            "business_lines": len(by_bl),
            "recent_changed": len(recent),
            "red_dot_apps": len({a["id"] for a in missing_owner} | {a["id"] for a in missing_env}),
        },
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 静态页面

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
