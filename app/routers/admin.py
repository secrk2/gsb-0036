"""织云系统 - 权限管理与应用交接 API。

- 角色调整：仅平台管理员；
- 授权/收权（业务线 × 环境，密文查看权与配置编辑权分开）：平台管理员或该业务线负责人；
- 应用交接：平台管理员 / 业务线负责人 / 应用当前负责人可发起；
  交接的是应用归属与随之而来的配置管理权限，配置项随应用一并移交（无需搬数据），
  交接前后负责人、发起人、时间、备注全部留痕；
- 权限变更本身写 permission_logs，并在应用交接时同步写应用变更日志。
"""
import time

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import permissions as perms
from ..auth import User, err, get_app_or_404
from ..db import (
    ENV_LABELS, ENV_SCOPE_ALL, ROLES, ROLE_LABELS, execute, query, query_one,
)

router = APIRouter()


# ---------------------------------------------------------------- 请求模型

class RoleIn(BaseModel):
    role: str
    business_line_id: int | None = None


class GrantIn(BaseModel):
    business_line_id: int
    environment: str = ENV_SCOPE_ALL
    can_edit_config: bool = False
    can_reveal: bool = False


class TransferIn(BaseModel):
    new_owner_id: int
    note: str = Field(default="", max_length=200)


# ---------------------------------------------------------------- 辅助

def _env_label(env: str) -> str:
    if env == ENV_SCOPE_ALL:
        return "全部环境"
    # 自定义环境键：展示名取该业务线任一应用注册表里的名字，取不到回退键本身
    label = ENV_LABELS.get(env)
    if label:
        return label
    return env


def _grant_row_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "user_id": r["user_id"],
        "business_line_id": r["business_line_id"],
        "business_line_name": r["business_line_name"],
        "environment": r["environment"],
        "environment_label": _env_label(r["environment"]),
        "can_view_config": bool(r["can_view_config"]),
        "can_edit_config": bool(r["can_edit_config"]),
        "can_reveal": bool(r["can_reveal"]),
        "granted_by_name": r["granted_by_name"],
        "updated_at": r["updated_at"],
    }


def _load_user_or_404(user_id: int) -> dict:
    row = query_one(
        """SELECT u.*, b.name AS business_line_name
           FROM users u LEFT JOIN business_lines b ON b.id = u.business_line_id
           WHERE u.id = ?""",
        (user_id,),
    )
    if not row:
        raise err(404, f"用户 #{user_id} 不存在")
    return dict(row)


def _ensure_can_read_perms(actor: dict, target: dict) -> None:
    if perms.is_admin(actor) or actor["id"] == target["id"]:
        return
    if actor["role"] == "bl_owner" and target.get("business_line_id") == actor["business_line_id"]:
        return
    raise err(403, "你只能查看本业务线成员或自己的权限明细")


# ---------------------------------------------------------------- 权限明细查询

@router.get("/api/admin/users/{user_id}/permissions")
def get_user_permissions(user_id: int, user: dict = User):
    target = _load_user_or_404(user_id)
    _ensure_can_read_perms(user, target)
    grants = query(
        """SELECT g.*, b.name AS business_line_name,
                  au.name AS granted_by_name
           FROM user_grants g
           JOIN business_lines b ON b.id = g.business_line_id
           LEFT JOIN users au ON au.id = g.granted_by
           WHERE g.user_id = ?
           ORDER BY g.business_line_id, g.environment""",
        (user_id,),
    )
    owned = query(
        """SELECT a.id, a.name, a.business_line_id, b.name AS business_line_name
           FROM applications a JOIN business_lines b ON b.id = a.business_line_id
           WHERE a.owner_id = ? ORDER BY a.id""",
        (user_id,),
    )
    return {
        "user": {
            "id": target["id"], "name": target["name"], "username": target["username"],
            "role": target["role"], "role_label": ROLE_LABELS.get(target["role"], target["role"]),
            "business_line_id": target["business_line_id"],
            "business_line_name": target.get("business_line_name"),
        },
        "grants": [_grant_row_to_dict(g) for g in grants],
        "owned_apps": [dict(r) for r in owned],
        "can_manage": perms.is_admin(user)
                      or (user["role"] == "bl_owner"
                          and target.get("business_line_id") == user["business_line_id"]),
        "can_assign_role": perms.can_assign_roles(user),
    }


# ---------------------------------------------------------------- 角色调整

@router.put("/api/admin/users/{user_id}/role")
def change_user_role(user_id: int, body: RoleIn, actor: dict = User):
    if not perms.can_assign_roles(actor):
        raise err(403, "只有平台管理员可以调整账号角色，业务线负责人可在本业务线内授权但不能改角色")
    if body.role not in ROLES:
        raise err(400, f"非法角色：{body.role}，可选：{'/'.join(ROLE_LABELS[r] for r in ROLES)}")
    target = _load_user_or_404(user_id)

    bl_id = body.business_line_id
    if body.role in ("bl_owner", "app_owner") and not bl_id:
        # 允许沿用成员原有业务线
        bl_id = target.get("business_line_id")
        if not bl_id:
            raise err(400, f"设为「{ROLE_LABELS[body.role]}」必须指定所属业务线")
    if bl_id:
        if not query_one("SELECT id FROM business_lines WHERE id = ?", (bl_id,)):
            raise err(400, f"业务线 #{bl_id} 不存在")

    if body.role == "admin":
        bl_id = None  # 平台管理员不绑定业务线

    # 防止撤销最后一个平台管理员导致系统无人能管
    if target["role"] == "admin" and body.role != "admin":
        n = query_one("SELECT COUNT(*) AS c FROM users WHERE role = 'admin'")["c"]
        if n <= 1:
            raise err(400, "系统至少保留一个平台管理员，不能撤销最后一个管理员角色")
    if actor["id"] == target["id"] and body.role != "admin" and target["role"] == "admin":
        raise err(400, "不能降低你自己的平台管理员角色，请由其他管理员操作")

    old_label = ROLE_LABELS.get(target["role"], target["role"])
    new_label = ROLE_LABELS[body.role]
    execute("UPDATE users SET role = ?, business_line_id = ? WHERE id = ?",
            (body.role, bl_id, user_id))
    detail = f"角色调整：{old_label} → {new_label}"
    if bl_id and target.get("business_line_id") != bl_id:
        bl = query_one("SELECT name FROM business_lines WHERE id = ?", (bl_id,))
        detail += f"；所属业务线设为「{bl['name']}」"
    perms.log_permission(actor["id"], user_id, "role_change", "", detail)
    return {"ok": True, "detail": detail}


# ---------------------------------------------------------------- 授权 / 收权

def _validate_grant_target(actor: dict, target: dict, bl_id: int, env: str) -> None:
    if not query_one("SELECT id FROM business_lines WHERE id = ?", (bl_id,)):
        raise err(400, f"业务线 #{bl_id} 不存在")
    if env == ENV_SCOPE_ALL:
        pass
    elif env in ENV_LABELS:
        pass
    else:
        # 自定义环境：允许对该业务线下应用已注册的自定义环境授权
        import re as _re
        if not _re.fullmatch(r"[a-z0-9_-]{1,32}", env or ""):
            raise err(400, f"非法环境：{env}")
        row = query_one(
            """SELECT e.env_label FROM app_environments e
               JOIN applications a ON a.id=e.app_id
               WHERE a.business_line_id=? AND e.env_key=? LIMIT 1""",
            (bl_id, env),
        )
        if not row:
            raise err(400, f"业务线下没有任何应用注册环境「{env}」，请确认环境标识")
    # 业务线负责人只能给本业务线授权
    perms.ensure_can_manage_bl(actor, bl_id)


@router.put("/api/admin/users/{user_id}/grants", status_code=201)
def upsert_grant(user_id: int, body: GrantIn, actor: dict = User):
    target = _load_user_or_404(user_id)
    _validate_grant_target(actor, target, body.business_line_id, body.environment)
    if target["role"] == "admin":
        raise err(400, "平台管理员默认拥有全部权限，无需也不能再单独授权")

    can_edit = 1 if body.can_edit_config else 0
    can_reveal = 1 if body.can_reveal else 0
    # 只读观察者角色封顶：编辑权授了也不生效，服务端直接拒收并说明
    if target["role"] == "viewer" and can_edit:
        raise err(400, "该账号是「只读观察者」，角色本身禁止任何编辑；"
                       "如需开放编辑，请先由平台管理员把角色调整为应用负责人，密文查看权仍可单独授予")

    bl = query_one("SELECT name FROM business_lines WHERE id = ?", (body.business_line_id,))
    now = int(time.time())
    existing = query_one(
        "SELECT * FROM user_grants WHERE user_id = ? AND business_line_id = ? AND environment = ?",
        (user_id, body.business_line_id, body.environment),
    )
    scope_text = f"{bl['name']} · {_env_label(body.environment)}"
    if existing:
        execute(
            """UPDATE user_grants SET can_view_config=1, can_edit_config=?, can_reveal=?,
                                      granted_by=?, updated_at=?
               WHERE id=?""",
            (can_edit, can_reveal, actor["id"], now, existing["id"]),
        )
        changes = []
        if bool(existing["can_edit_config"]) != bool(can_edit):
            changes.append(f"配置编辑权 {'授予' if can_edit else '收回'}")
        if bool(existing["can_reveal"]) != bool(can_reveal):
            changes.append(f"密文查看权 {'授予' if can_reveal else '收回'}")
        detail = f"调整 {target['name']} 在「{scope_text}」的权限：" + \
                 ("；".join(changes) if changes else "权限无变化（已存在相同授权）")
        action = "grant"
        status_code = 200
    else:
        execute(
            """INSERT INTO user_grants
               (user_id, business_line_id, environment, can_view_config,
                can_edit_config, can_reveal, granted_by, created_at, updated_at)
               VALUES (?,?,?,1,?,?,?,?,?)""",
            (user_id, body.business_line_id, body.environment, can_edit, can_reveal,
             actor["id"], now, now),
        )
        flags = ["配置查看（脱敏）"]
        if can_edit:
            flags.append("配置编辑")
        if can_reveal:
            flags.append("密文查看明文")
        detail = f"授予 {target['name']} 在「{scope_text}」的权限：{'、'.join(flags)}"
        action = "grant"
        status_code = 201
    perms.log_permission(actor["id"], user_id, action, scope_text, detail)
    return {"ok": True, "detail": detail}


@router.delete("/api/admin/users/{user_id}/grants/{grant_id}")
def revoke_grant(user_id: int, grant_id: int, actor: dict = User):
    target = _load_user_or_404(user_id)
    row = query_one("SELECT * FROM user_grants WHERE id = ? AND user_id = ?", (grant_id, user_id))
    if not row:
        raise err(404, f"授权记录 #{grant_id} 不存在或不属于该用户")
    perms.ensure_can_manage_bl(actor, row["business_line_id"])
    bl = query_one("SELECT name FROM business_lines WHERE id = ?", (row["business_line_id"],))
    flags = []
    if row["can_edit_config"]:
        flags.append("配置编辑权")
    if row["can_reveal"]:
        flags.append("密文查看权")
    flags.append("配置查看权")
    execute("DELETE FROM user_grants WHERE id = ?", (grant_id,))
    scope_text = f"{bl['name']} · {_env_label(row['environment'])}"
    detail = f"收回 {target['name']} 在「{scope_text}」的全部授权（含{'、'.join(flags)}）"
    perms.log_permission(actor["id"], user_id, "revoke", scope_text, detail)
    return {"ok": True, "detail": detail}


# ---------------------------------------------------------------- 应用交接

@router.post("/api/apps/{app_id}/transfer", status_code=201)
def transfer_app(app_id: int, body: TransferIn, actor: dict = User):
    app_row = get_app_or_404(app_id)
    bl_id = app_row["business_line_id"]
    is_current_owner = app_row["owner_id"] == actor["id"] and actor["role"] != "viewer"
    if not (perms.can_manage_bl(actor, bl_id) or is_current_owner):
        raise err(403, "只有平台管理员、业务线负责人或应用当前负责人可以发起交接")

    new_owner = query_one(
        "SELECT id, name, role, business_line_id FROM users WHERE id = ?",
        (body.new_owner_id,),
    )
    if not new_owner:
        raise err(400, "交接目标负责人不存在")
    if new_owner["role"] == "admin":
        raise err(400, "平台管理员不绑定具体应用，不能作为应用负责人接收交接")
    if new_owner["role"] == "viewer":
        raise err(400, "只读观察者不能作为应用负责人接收交接，请先调整其角色")
    if new_owner["business_line_id"] != bl_id:
        bl = query_one("SELECT name FROM business_lines WHERE id = ?", (bl_id,))
        raise err(400, f"新负责人必须属于应用所在业务线「{bl['name']}」；"
                       f"跨业务线交接请先由平台管理员调整人员归属")
    if new_owner["id"] == app_row["owner_id"]:
        raise err(400, "该应用已经由目标账号负责，无需重复交接")

    note = body.note.strip()
    old_owner_id = app_row["owner_id"]
    old_name = query_one("SELECT name FROM users WHERE id = ?", (old_owner_id,)) if old_owner_id else None
    now = int(time.time())

    # 交接的是应用归属与权限：配置项（config_items / 全部历史版本 / 留痕）随应用一并移交，
    # 数据本身挂在 app_id 上，无需搬动；owner 一改，新负责人立即获得该应用的配置查看/编辑面。
    execute("UPDATE applications SET owner_id = ?, updated_at = ? WHERE id = ?",
            (new_owner["id"], now, app_id))
    execute(
        """INSERT INTO app_transfers
           (app_id, old_owner_id, new_owner_id, transfer_by_id, note, created_at)
           VALUES (?,?,?,?,?,?)""",
        (app_id, old_owner_id, new_owner["id"], actor["id"], note, now),
    )
    old_txt = old_name["name"] if old_name else "（空缺）"
    summary = (f"应用交接：负责人 {old_txt} → {new_owner['name']}"
               + (f"；交接备注：{note}" if note else "")
               + "；配置项与全部历史版本、留痕随应用一并移交")
    execute(
        "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (app_id, actor["id"], "应用交接", summary, now),
    )
    perms.log_permission(
        actor["id"], new_owner["id"], "transfer",
        f"应用「{app_row['name']}」",
        f"{summary}（交接前负责人：{old_txt}，交接后负责人：{new_owner['name']}）",
    )
    return {
        "ok": True,
        "app_id": app_id,
        "old_owner_name": old_txt,
        "new_owner_name": new_owner["name"],
        "detail": summary,
    }


@router.get("/api/admin/transfers")
def list_transfers(user: dict = User, app_id: int | None = None):
    """交接留痕：管理员全部；业务线负责人本业务线；其他人只看与自己相关的交接。"""
    sql = """SELECT t.*, a.name AS app_name, a.business_line_id, b.name AS business_line_name,
                    ou.name AS old_owner_name, nu.name AS new_owner_name,
                    du.name AS transfer_by_name
             FROM app_transfers t
             JOIN applications a ON a.id = t.app_id
             JOIN business_lines b ON b.id = a.business_line_id
             LEFT JOIN users ou ON ou.id = t.old_owner_id
             LEFT JOIN users nu ON nu.id = t.new_owner_id
             LEFT JOIN users du ON du.id = t.transfer_by_id
             WHERE 1=1"""
    params: list = []
    if not perms.is_admin(user):
        ors, p = [], []
        if user["role"] == "bl_owner" and user.get("business_line_id"):
            ors.append("a.business_line_id = ?")
            p.append(user["business_line_id"])
        ors.append("(t.old_owner_id = ? OR t.new_owner_id = ?)")
        p.extend([user["id"], user["id"]])
        sql += " AND (" + " OR ".join(ors) + ")"
        params.extend(p)
    if app_id:
        sql += " AND t.app_id = ?"
        params.append(app_id)
    sql += " ORDER BY t.created_at DESC, t.id DESC LIMIT 200"
    return [dict(r) for r in query(sql, tuple(params))]


# ---------------------------------------------------------------- 权限变更留痕

@router.get("/api/admin/permission-logs")
def list_permission_logs(user: dict = User, target_user_id: int | None = None):
    """权限与交接类留痕查询。

    - 平台管理员：全部；
    - 业务线负责人：本业务线成员的权限记录 + 自己经手/相关的记录；
    - 其他角色：只看与自己账号相关的（被授权/被收权/被交接）。
    """
    sql = """SELECT l.*, au.name AS actor_name, tu.name AS target_name
             FROM permission_logs l
             LEFT JOIN users au ON au.id = l.actor_id
             LEFT JOIN users tu ON tu.id = l.target_user_id
             WHERE 1=1"""
    params: list = []
    if perms.is_admin(user):
        if target_user_id:
            sql += " AND l.target_user_id = ?"
            params.append(target_user_id)
    elif user["role"] == "bl_owner" and user.get("business_line_id"):
        sql += """ AND (
            l.target_user_id IN (SELECT id FROM users WHERE business_line_id = ?)
            OR l.actor_id = ? OR l.target_user_id = ?)"""
        params.extend([user["business_line_id"], user["id"], user["id"]])
        if target_user_id:
            sql += " AND l.target_user_id = ?"
            params.append(target_user_id)
    else:
        sql += " AND (l.actor_id = ? OR l.target_user_id = ?)"
        params.extend([user["id"], user["id"]])
    sql += " ORDER BY l.created_at DESC, l.id DESC LIMIT 300"
    action_labels = {"grant": "授权", "revoke": "收权", "role_change": "角色调整", "transfer": "应用交接"}
    rows = query(sql, tuple(params))
    return [{
        "id": r["id"],
        "actor_name": r["actor_name"] or "系统",
        "target_name": r["target_name"] or "（已删除账号）",
        "action": r["action"],
        "action_label": action_labels.get(r["action"], r["action"]),
        "scope_text": r["scope_text"],
        "detail": r["detail"],
        "created_at": r["created_at"],
    } for r in rows]
