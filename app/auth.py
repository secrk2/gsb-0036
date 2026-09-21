"""织云系统 - 认证、主体装载、应用取数与越权守卫。

主体（current_user）在登录态校验时一次性装载授权明细与负责应用集合，
权限判定统一走 permissions 模块；所有越权都抛出带具体原因的 403。
"""
from fastapi import Depends, Header, HTTPException

from . import permissions as perms
from .db import query_one


def err(status_code: int, message: str, extra: dict | None = None) -> HTTPException:
    if extra:
        return HTTPException(status_code=status_code, detail={"message": message, **extra})
    return HTTPException(status_code=status_code, detail=message)


def current_user(x_token: str = Header(default="")) -> dict:
    if not x_token:
        raise err(401, "未登录：缺少访问令牌")
    row = query_one(
        """SELECT u.id, u.username, u.name, u.role, u.business_line_id,
                  b.name AS business_line_name, u.token
           FROM users u LEFT JOIN business_lines b ON b.id = u.business_line_id
           WHERE u.token = ?""",
        (x_token,),
    )
    if not row:
        raise err(401, "登录已失效，请重新登录")
    return perms.build_principal(dict(row))


User = Depends(current_user)


def public_user(user: dict) -> dict:
    """剥离内部缓存字段后的对外用户形态（/api/me、/api/login）。"""
    data = {k: v for k, v in user.items() if not k.startswith("_") and k != "token"}
    data["permissions"] = perms.permission_summary(user)
    return data


def is_admin(user: dict) -> bool:
    return perms.is_admin(user)


def is_bl_visible(user: dict, bl_id: int) -> bool:
    return perms.is_admin(user) or bl_id in perms.visible_business_lines(user)


def ensure_bl_visible(user: dict, bl_id: int) -> None:
    """读场景：业务线不在任何可见范围 → 403 并说明当前可访问范围。"""
    if is_bl_visible(user, bl_id):
        return
    raise err(403, perms.deny_reason(user, bl_id, None, "view"))


def check_bl_scope(user: dict, business_line_id: int) -> None:
    """兼容旧调用：等价于读场景的业务线可见性校验。"""
    ensure_bl_visible(user, business_line_id)


def get_app_or_404(app_id: int) -> dict:
    row = query_one("SELECT * FROM applications WHERE id = ?", (app_id,))
    if not row:
        raise err(404, f"应用 #{app_id} 不存在或已被删除")
    return dict(row)


def get_app_checked(user: dict, app_id: int) -> dict:
    """应用详情/配置读：过 应用 + 所属环境 可见范围。"""
    app_row = get_app_or_404(app_id)
    perms.ensure_app_visible(user, app_row)
    return app_row


def get_app_writable(user: dict, app_id: int, what: str = "应用信息") -> dict:
    """台账写操作（改信息/状态/环境变量）：管理员、本业务线负责人、应用负责人本人。

    只读观察者与无编辑权账号一律拦下并说明原因。
    """
    app_row = get_app_or_404(app_id)
    if perms.is_admin(user) or perms.is_bl_owner(user, app_row["business_line_id"]):
        return app_row
    if app_row["id"] in user.get("_owned_apps", set()) and user["role"] != "viewer":
        return app_row
    if user["role"] == "viewer":
        raise err(403, f"禁止修改{what}：你的账号是「只读观察者」，仅可查看授权范围内的数据")
    raise err(
        403,
        f"禁止修改{what}：你不是应用「{app_row['name']}」的负责人，也不是其所属业务线的负责人；"
        f"配置与台账编辑权按应用归属与授权收窄，请联系业务线负责人办理交接或授权。",
    )
