"""织云系统 - 权限与可见范围。

角色（四类）：
- admin     平台管理员：全部业务线 × 全部环境，查看/编辑/密文/授权/交接全部可做；
- bl_owner  业务线负责人：本业务线全部环境全权，可在本业务线内授权与发起交接；
- app_owner 应用负责人：本人负责的应用全部环境可见、可编辑，但密文查看必须另行授予；
- viewer    只读观察者：只能看到被显式授权的 业务线×环境，永远不能编辑（角色封顶）。

可见范围按 业务线 × 环境 两级收窄（user_grants，environment='*' 表示该业务线全部环境）。
密文查看权（can_reveal）与配置编辑权（can_edit_config）分开授予：
能看到明文不代表能改，能改也不代表能看明文。

所有越权一律抛出带具体原因的 403：缺的是业务线范围、环境范围还是某项权限，
让前端能把原因展示出来，而不是给用户一个空白页。
"""
import time

from .db import (
    ENV_LABELS, ENV_SCOPE_ALL, ROLE_LABELS, execute, query, query_one,
)

PERM_COLS = {
    "view": "can_view_config",
    "edit": "can_edit_config",
    "reveal": "can_reveal",
}


def env_label_of(env: str | None) -> str:
    """环境展示名：内置四环境走全局字典，应用自定义环境回退为键本身（各接口可再按应用覆盖）。"""
    if not env:
        return ""
    return ENV_LABELS.get(env, env)


# ---------------------------------------------------------------- 主体资料装载

def build_principal(row: dict) -> dict:
    """把 users 行扩展为会话主体：附带授权明细与负责应用，供各守卫零额外查询使用。"""
    user = dict(row)
    if not user.get("business_line_name") and user.get("business_line_id"):
        bl = query_one("SELECT name FROM business_lines WHERE id = ?", (user["business_line_id"],))
        user["business_line_name"] = bl["name"] if bl else None
    user["role_label"] = ROLE_LABELS.get(user["role"], user["role"])
    grants = [dict(g) for g in query(
        """SELECT g.*, b.name AS business_line_name
           FROM user_grants g JOIN business_lines b ON b.id = g.business_line_id
           WHERE g.user_id = ? ORDER BY g.business_line_id, g.environment""",
        (user["id"],),
    )]
    user["_grants"] = grants
    user["_owned_apps"] = {r["id"] for r in query(
        "SELECT id FROM applications WHERE owner_id = ?", (user["id"],)
    )}
    return user


def is_admin(user: dict) -> bool:
    return user["role"] == "admin"


def role_label(user: dict) -> str:
    return ROLE_LABELS.get(user["role"], user["role"])


def grants_of(user: dict) -> list[dict]:
    return user.get("_grants") if "_grants" in user else [
        dict(g) for g in query("SELECT * FROM user_grants WHERE user_id = ?", (user["id"],))
    ]


# ---------------------------------------------------------------- 范围判定

def is_bl_owner(user: dict, bl_id: int) -> bool:
    return user["role"] == "bl_owner" and user["business_line_id"] == bl_id


def _grant_match(grants: list[dict], bl_id: int, env: str | None, perm: str):
    """命中某 (业务线, 环境) 上某项权限的授权行；env=None 时只匹配整业务线('*')授权。"""
    col = PERM_COLS[perm]
    for g in grants:
        if g["business_line_id"] != bl_id or not g[col]:
            continue
        if g["environment"] == ENV_SCOPE_ALL or (env is not None and g["environment"] == env):
            return g
    return None


def _granted_envs(grants: list[dict], bl_id: int, perm: str = "view") -> list[str]:
    """该用户在某业务线上被授予某项权限的环境集合（已展开 '*'）。

    '*' 授权语义上覆盖该业务线全部环境（含应用自定义环境）；这里只返回已知的
    内置环境键用于拒绝原因展示，自定义键在具体应用上下文里由注册表判定。
    """
    col = PERM_COLS[perm]
    envs: set[str] = set()
    wildcard = False
    for g in grants:
        if g["business_line_id"] == bl_id and g[col]:
            if g["environment"] == ENV_SCOPE_ALL:
                wildcard = True
            envs.add(g["environment"])
    if wildcard:
        # 内置四环境 + 该业务线上出现过的自定义环境键（授权留痕/注册表可能引用）
        rows = query(
            """SELECT DISTINCT e.env_key FROM app_environments e
               JOIN applications a ON a.id=e.app_id
               WHERE a.business_line_id=?""",
            (bl_id,),
        )
        envs.update(r["env_key"] for r in rows)
    order = {k: i for i, k in enumerate(ENV_LABELS)}
    return sorted(envs, key=lambda k: (order.get(k, 99), k))


def visible_business_lines(user: dict) -> set[int]:
    """用户任意一点可见范围涉及的业务线（用于区分"业务线级越权"与"环境级越权"）。"""
    bls = {g["business_line_id"] for g in grants_of(user)}
    if user.get("business_line_id"):
        bls.add(user["business_line_id"])
    if user["_owned_apps"]:
        rows = query("SELECT DISTINCT business_line_id FROM applications WHERE owner_id = ?",
                     (user["id"],))
        bls.update(r["business_line_id"] for r in rows)
    return bls


def scope_condition(user: dict, bl_col: str, env_col: str,
                    owner_col: str | None = None) -> tuple[str, list]:
    """生成"可见范围"SQL 片段（OR 组合），供列表/留痕/档案查询复用。

    - admin：无约束（1=1）；
    - 业务线负责人：本业务线全部环境；
    - 应用归属：本人负责的应用（全部环境）；
    - 授权：user_grants 中 can_view_config=1 的 业务线×环境。
    """
    if is_admin(user):
        return "1=1", []
    ors: list[str] = []
    params: list = []
    if owner_col is not None:
        ors.append(f"{owner_col} = ?")
        params.append(user["id"])
    if user["role"] == "bl_owner" and user.get("business_line_id"):
        ors.append(f"{bl_col} = ?")
        params.append(user["business_line_id"])
    for g in grants_of(user):
        if not g["can_view_config"]:
            continue
        if g["environment"] == ENV_SCOPE_ALL:
            ors.append(f"{bl_col} = ?")
            params.append(g["business_line_id"])
        else:
            ors.append(f"({bl_col} = ? AND {env_col} = ?)")
            params.extend([g["business_line_id"], g["environment"]])
    return ("(" + " OR ".join(ors) + ")") if ors else "0=1", params


# ---------------------------------------------------------------- 越权原因（说人话）

def _bl_name(bl_id: int) -> str:
    row = query_one("SELECT name FROM business_lines WHERE id = ?", (bl_id,))
    return row["name"] if row else f"#{bl_id}"


def env_label_in_bl(bl_id: int, env: str | None) -> str:
    """某业务线语境下的环境展示名：内置走全局字典，自定义环境取注册表里的名字。"""
    if not env:
        return ""
    label = ENV_LABELS.get(env)
    if label:
        return label
    row = query_one(
        """SELECT e.env_label FROM app_environments e
           JOIN applications a ON a.id=e.app_id
           WHERE a.business_line_id=? AND e.env_key=? LIMIT 1""",
        (bl_id, env),
    )
    return row["env_label"] if row else env


def deny_reason(user: dict, bl_id: int, env: str | None, perm: str,
                owns_app: bool = False) -> str:
    """生成越权说明：业务线级 / 环境级 / 权限级 三层原因。

    owns_app 表示用户是否拥有"本次被访问的那个具体应用"（而非该业务线下任意应用）：
    拥有同业务线下的别的应用不构成对目标 应用×环境 的放行。
    """
    who = role_label(user)
    bl_name = _bl_name(bl_id)
    grants = grants_of(user)

    # 1) 整条业务线都不在任何可见范围内
    if bl_id not in visible_business_lines(user):
        mine = sorted({g["business_line_name"] for g in grants})
        if user["role"] == "bl_owner" and user.get("business_line_name"):
            mine.append(user["business_line_name"])
        mine_txt = "、".join(mine) if mine else "暂无任何业务线"
        return (f"越权访问：你的角色是「{who}」，可见范围不含业务线「{bl_name}」"
                f"（你当前可访问：{mine_txt}）。如需访问，请联系业务线负责人或平台管理员授权。")

    # 2) 业务线在范围内，但目标环境没授权（拥有本次访问的具体应用时除外——那种情况下不会被拦到这里）
    view_envs = _granted_envs(grants, bl_id, "view")
    if env is not None and not is_bl_owner(user, bl_id) and not owns_app and env not in view_envs:
        env_txt = "、".join(env_label_in_bl(bl_id, e) for e in view_envs) if view_envs else "无"
        return (f"越权访问：你在业务线「{bl_name}」只有【{env_txt}】环境的访问权，"
                f"【{env_label_in_bl(bl_id, env)}】环境不在授权范围内。")

    # 3) 环境能看，但缺具体权限
    env_name = env_label_in_bl(bl_id, env) if env else ""
    if perm == "edit":
        if user["role"] == "viewer":
            return (f"禁止修改：你的账号是「只读观察者」，对「{bl_name}"
                    + (f"·{env_name}" if env else "")
                    + "」只有查看权，没有配置编辑权。")
        return (f"禁止修改：你的账号在「{bl_name}"
                f"·{env_name if env else '全部环境'}」只有查看权（密文脱敏展示），"
                f"没有配置编辑权；能查看不等于能修改。请联系业务线负责人或平台管理员授予编辑权。")
    if perm == "reveal":
        return (f"禁止查看明文：你的账号在「{bl_name}"
                f"·{env_name if env else '全部环境'}」没有密文查看权。"
                f"密文查看权与配置查看/编辑权分开授予，能看脱敏值或能改配置都不代表能看明文，"
                f"请向业务线负责人或平台管理员单独申请密文查看权。")
    return f"越权访问：你没有「{bl_name}」的访问权限。"


# ---------------------------------------------------------------- 单资源守卫

def ensure_app_visible(user: dict, app_row: dict) -> None:
    """应用台账（按应用所属环境判定）可见性。"""
    if is_admin(user) or is_bl_owner(user, app_row["business_line_id"]):
        return
    owns = app_row["id"] in user.get("_owned_apps", set())
    grants = grants_of(user)
    if owns or _grant_match(grants, app_row["business_line_id"], app_row["environment"], "view"):
        return
    from .auth import err
    raise err(403, deny_reason(user, app_row["business_line_id"], app_row["environment"],
                               "view", owns_app=owns))


def ensure_config_perm(user: dict, app_row: dict, env: str, perm: str) -> None:
    """配置档案（按 应用 + 配置环境 判定）的 view/edit/reveal 守卫。"""
    # 平台管理员与应用所属业务线负责人：该应用全环境三项权限全放行
    if is_admin(user) or is_bl_owner(user, app_row["business_line_id"]):
        return
    from .auth import err
    bl_id = app_row["business_line_id"]
    grants = grants_of(user)
    is_owner = app_row["id"] in user.get("_owned_apps", set())

    if perm == "view":
        ok = is_owner or bool(_grant_match(grants, bl_id, env, "view"))
    elif perm == "edit":
        if user["role"] == "viewer":
            raise err(403, deny_reason(user, bl_id, env, "edit", owns_app=is_owner))
        ok = is_owner or bool(_grant_match(grants, bl_id, env, "edit"))
    elif perm == "reveal":
        # 应用负责人默认也不能看明文：密文权必须单独授予
        ok = bool(_grant_match(grants, bl_id, env, "reveal"))
    else:  # pragma: no cover - 防御
        ok = False
    if not ok:
        raise err(403, deny_reason(user, bl_id, env, perm, owns_app=is_owner))


def can_reveal(user: dict, bl_id: int, env: str) -> bool:
    if is_admin(user) or is_bl_owner(user, bl_id):
        return True
    return bool(_grant_match(grants_of(user), bl_id, env, "reveal"))


def has_bl_env_view(user: dict, bl_id: int, env: str) -> bool:
    """业务线级聚合查询（跨该业务线多个应用）时，某环境是否有查看权。

    与"单个应用"判定不同：这里不认应用归属（拥有该业务线里的一个应用，
    不代表能看整条业务线其他应用在该环境的数据），只认 admin/bl_owner/显式授权。
    """
    return (is_admin(user) or is_bl_owner(user, bl_id)
            or bool(_grant_match(grants_of(user), bl_id, env, "view")))


def can_edit_config(user: dict, app_row: dict, env: str) -> bool:
    if is_admin(user) or is_bl_owner(user, app_row["business_line_id"]):
        return True
    if user["role"] == "viewer":
        return False
    return (app_row["id"] in user.get("_owned_apps", set())
            or bool(_grant_match(grants_of(user), app_row["business_line_id"], env, "edit")))


def can_manage_app(user: dict, app_row: dict) -> bool:
    """台账写操作（改信息/状态流转/环境变量）：管理员、本业务线负责人、应用负责人本人。"""
    if is_admin(user) or is_bl_owner(user, app_row["business_line_id"]):
        return True
    return user["role"] != "viewer" and app_row["id"] in user.get("_owned_apps", set())


# ---------------------------------------------------------------- 管理类权限

def can_manage_bl(user: dict, bl_id: int) -> bool:
    """授权/收权/交接的管理范围：平台管理员任意；业务线负责人仅限本业务线。"""
    return is_admin(user) or is_bl_owner(user, bl_id)


def ensure_can_manage_bl(user: dict, bl_id: int) -> None:
    from .auth import err
    if not can_manage_bl(user, bl_id):
        if is_admin(user) is False and user["role"] == "bl_owner":
            raise err(403, f"越权操作：业务线负责人只能管理本业务线（{user.get('business_line_name')}）的权限与交接")
        raise err(403, "权限管理仅限平台管理员与业务线负责人，你的账号没有授权/交接的管理权限")


def can_assign_roles(user: dict) -> bool:
    """只有平台管理员能调整账号角色。"""
    return is_admin(user)


# ---------------------------------------------------------------- 权限摘要（给 /api/me 与前端）

def permission_summary(user: dict) -> dict:
    scopes = []
    for g in grants_of(user):
        scopes.append({
            "business_line_id": g["business_line_id"],
            "business_line_name": g["business_line_name"],
            "environment": g["environment"],
            "environment_label": "全部环境" if g["environment"] == ENV_SCOPE_ALL else ENV_LABELS[g["environment"]],
            "can_view_config": bool(g["can_view_config"]),
            "can_edit_config": bool(g["can_edit_config"]) and user["role"] != "viewer",
            "can_reveal": bool(g["can_reveal"]),
        })
    owned = []
    if user.get("_owned_apps"):
        rows = query(
            "SELECT a.id, a.name, a.business_line_id, b.name AS business_line_name "
            "FROM applications a JOIN business_lines b ON b.id = a.business_line_id "
            "WHERE a.owner_id = ? ORDER BY a.id",
            (user["id"],),
        )
        owned = [dict(r) for r in rows]
    return {
        "role": user["role"],
        "role_label": role_label(user),
        "is_admin": is_admin(user),
        "can_manage_all": is_admin(user),
        "scopes": scopes,
        "owned_apps": owned,
    }


# ---------------------------------------------------------------- 权限留痕

def log_permission(actor_id: int, target_user_id: int, action: str,
                   scope_text: str, detail: str) -> None:
    execute(
        """INSERT INTO permission_logs
           (actor_id, target_user_id, action, scope_text, detail, created_at)
           VALUES (?,?,?,?,?,?)""",
        (actor_id, target_user_id, action, scope_text, detail, int(time.time())),
    )
