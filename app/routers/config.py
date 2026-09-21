"""织云系统 - 配置档案 & 变更留痕 API。

所有接口都走 current_user 鉴权，并按"业务线 × 环境 + 应用归属"做可见范围收口；
密文查看权（reveal）与配置编辑权（edit）分开校验，互不蕴涵；
密文相关规则在服务端强制执行（仅前端拦截不算数）；
配置保存带 expected_version 乐观锁，并发改动冲突时返回 409 与逐键差异，绝不静默覆盖。
"""
import time
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import config_service as svc
from .. import env_service as esvc
from .. import permissions as perms
from ..auth import User, ensure_bl_visible, err, get_app_or_404
from ..db import (
    CONFIG_SCOPE_LABELS, CONFIG_SCOPES, CONFIG_TYPE_LABELS, CONFIG_TYPES,
    TERMINAL_STATUS, query, query_one,
)

router = APIRouter()

REVEAL_REASON_MIN = 5


# ---------------------------------------------------------------- 请求模型

class ConfigItemIn(BaseModel):
    key: str
    value: str | None = ""
    value_type: str = "string"
    scope: str = "global"
    is_secret: bool = False
    keep_value: bool = False  # 密文编辑时留空：沿用库里旧值，避免明文回填


class SaveConfigIn(BaseModel):
    items: list[ConfigItemIn]
    change_note: str = Field(default="", max_length=200)
    # 编辑者打开编辑框时的当前版本号；服务端比对最新版，不一致即 409 冲突
    expected_version: int | None = None


class RevealIn(BaseModel):
    item_id: int
    reason: str = Field(default="", max_length=200)


class RollbackIn(BaseModel):
    environment: str
    version: int
    expected_version: int | None = None


# ---------------------------------------------------------------- 辅助

def env_checked(environment: str) -> str:
    """环境键的基本形态校验（环境已从写死枚举改为每应用注册表，具体归属在各接口按应用校验）。"""
    import re as _re
    if not _re.fullmatch(r"[a-z0-9_-]{1,32}", environment or ""):
        raise err(400, f"非法环境标识：{environment}")
    return environment


def app_env_or_400(app_id: int, environment: str):
    """校验该环境确实注册在该应用下，返回 app_environments 行。"""
    env_checked(environment)
    row = query_one(
        "SELECT * FROM app_environments WHERE app_id=? AND env_key=?",
        (app_id, environment),
    )
    if not row:
        raise err(400, f"该应用下不存在环境「{environment}」，请先在环境管理中新增")
    return row


def updater_names(item_rows: list[dict]) -> dict[int, str]:
    ids = {r["updated_by"] for r in item_rows if r["updated_by"]}
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = query(f"SELECT id, name FROM users WHERE id IN ({placeholders})", tuple(ids))
    return {r["id"]: r["name"] for r in rows}


def config_perms(user: dict, app_row: dict, env: str) -> dict:
    """当前账号在该 应用×环境 上的权限面与无权原因（供前端渲染按钮/说明条）。"""
    can_view = True  # 能进到这里说明 view 已通过
    edit_ok = perms.can_edit_config(user, app_row, env)
    reveal_ok = perms.can_reveal(user, app_row["business_line_id"], env)
    return {
        "can_view": can_view,
        "can_edit": edit_ok,
        "can_reveal": reveal_ok,
        "edit_deny_reason": None if edit_ok else perms.deny_reason(
            user, app_row["business_line_id"], env, "edit"),
        "reveal_deny_reason": None if reveal_ok else perms.deny_reason(
            user, app_row["business_line_id"], env, "reveal"),
    }


# ---------------------------------------------------------------- 元数据

@router.get("/api/config/meta")
def config_meta(user: dict = User):
    return {
        "types": [{"value": t, "label": CONFIG_TYPE_LABELS[t]} for t in CONFIG_TYPES],
        "scopes": [{"value": s, "label": CONFIG_SCOPE_LABELS[s]} for s in CONFIG_SCOPES],
        "actions": [{"value": k, "label": v} for k, v in svc.ACTION_LABELS.items()],
    }


# ---------------------------------------------------------------- 配置档案

@router.get("/api/config/profiles")
def list_profiles(user: dict = User,
                  business_line_id: int | None = None,
                  environment: str | None = None):
    """有配置档案（至少一个版本）的 应用×环境 列表，按可见范围收窄。"""
    sql = """SELECT a.id AS app_id, a.name AS app_name, a.status AS app_status,
                    a.owner_id,
                    b.id AS business_line_id, b.name AS business_line_name,
                    x.environment, x.version AS latest_version,
                    x.change_note AS latest_note, x.created_at AS latest_at,
                    u.name AS created_by_name,
                    (SELECT COUNT(*) FROM config_items ci
                       WHERE ci.app_id = a.id AND ci.environment = x.environment) AS item_count,
                    (SELECT COUNT(*) FROM config_items ci
                       WHERE ci.app_id = a.id AND ci.environment = x.environment AND ci.is_secret = 1) AS secret_count
             FROM config_versions x
             JOIN applications a ON a.id = x.app_id
             JOIN business_lines b ON b.id = a.business_line_id
             LEFT JOIN users u ON u.id = x.created_by
             WHERE x.version = (
                 SELECT MAX(y.version) FROM config_versions y
                 WHERE y.app_id = x.app_id AND y.environment = x.environment)"""
    params: list = []
    if perms.is_admin(user):
        if business_line_id:
            sql += " AND a.business_line_id = ?"
            params.append(business_line_id)
    else:
        # 显式按范围外业务线筛选属于越权：403 并说明可访问范围，而非静默空列表
        if business_line_id:
            ensure_bl_visible(user, business_line_id)
        cond, cond_params = perms.scope_condition(user, "a.business_line_id", "x.environment", "a.owner_id")
        sql += f" AND {cond}"
        params.extend(cond_params)
        if business_line_id:
            sql += " AND a.business_line_id = ?"
            params.append(business_line_id)
    if environment:
        env_checked(environment)
        sql += " AND x.environment = ?"
        params.append(environment)
    sql += " ORDER BY x.created_at DESC, a.id"
    return [dict(r) for r in query(sql, tuple(params))]


@router.get("/api/apps/{app_id}/config")
def get_config(app_id: int, environment: str, user: dict = User):
    app_row = get_app_or_404(app_id)
    env_row = app_env_or_400(app_id, environment)
    # 配置可见性按"配置环境"收窄（可能只授予了某条业务线的生产环境）
    perms.ensure_config_perm(user, app_row, environment, "view")
    rows = svc.current_items(app_id, environment)
    names = updater_names(rows)
    items = []
    for r in rows:
        d = svc.item_to_dict(r)
        d["updated_by_name"] = names.get(r["updated_by"])
        items.append(d)
    latest_no = query_one(
        "SELECT MAX(version) AS v FROM config_versions WHERE app_id = ? AND environment = ?",
        (app_id, environment),
    )["v"] or 0
    # 环境矩阵按该应用"已注册"的环境给出（含自定义环境）：前端据此渲染环境标签与置灰原因
    env_rows = query(
        "SELECT id, env_key, env_label FROM app_environments WHERE app_id=? ORDER BY "
        "CASE env_key WHEN 'dev' THEN 0 WHEN 'test' THEN 1 WHEN 'staging' THEN 2 "
        "WHEN 'prod' THEN 3 ELSE 4 END, id",
        (app_id,),
    )
    env_matrix = []
    for er in env_rows:
        env = er["env_key"]
        visible = (perms.is_admin(user) or perms.is_bl_owner(user, app_row["business_line_id"])
                   or app_row["id"] in user.get("_owned_apps", set())
                   or bool(perms._grant_match(perms.grants_of(user),
                                              app_row["business_line_id"], env, "view")))
        entry = {"environment": env, "environment_label": er["env_label"], "visible": visible}
        if visible:
            entry.update(config_perms(user, app_row, env))
        else:
            entry.update({
                "can_view": False, "can_edit": False, "can_reveal": False,
                "view_deny_reason": perms.deny_reason(
                    user, app_row["business_line_id"], env, "view"),
            })
        env_matrix.append(entry)
    # 当前环境发布窗口（配置页提示条 + 窗口外拦截依据）
    window = esvc.evaluate_window(app_env_or_400(app_id, environment))
    return {
        "app_id": app_id,
        "app_name": app_row["name"],
        "environment": environment,
        "read_only": app_row["status"] == TERMINAL_STATUS,
        "items": items,
        "versions": svc.list_versions(app_id, environment),
        "current_version": latest_no,
        "permissions": config_perms(user, app_row, environment),
        "env_permissions": env_matrix,
        "window": window,
    }


@router.put("/api/apps/{app_id}/config")
def save_config(app_id: int, environment: str, body: SaveConfigIn, user: dict = User):
    app_row = get_app_or_404(app_id)
    app_env_or_400(app_id, environment)
    # 编辑权独立于查看权与密文查看权：能看明文 ≠ 能改
    perms.ensure_config_perm(user, app_row, environment, "edit")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），配置档案只读，禁止修改")
    # 发布窗口卡口：窗口外的上线（配置发布）一律拦下，并告知下一次开放时间
    try:
        esvc.ensure_window_open(app_id, environment, actor_id=user["id"])
    except esvc.WindowClosedError as e:
        raise err(409, e.payload["message"], e.payload)
    try:
        items = svc.validate_items([it.model_dump() for it in body.items])
        result = svc.save_profile(app_id, environment, items, user,
                                  change_note=body.change_note,
                                  expected_version=body.expected_version)
    except svc.VersionConflictError as e:
        # 409 + 冲突详情：前端弹出差异与取舍，而不是让后来者静默覆盖
        raise err(409, e.payload["message"], e.payload)
    except ValueError as e:
        raise err(400, str(e))
    return {"ok": True, **result}


@router.post("/api/apps/{app_id}/config/rollback", status_code=201)
def rollback_config(app_id: int, body: RollbackIn, user: dict = User):
    app_row = get_app_or_404(app_id)
    app_env_or_400(app_id, body.environment)
    perms.ensure_config_perm(user, app_row, body.environment, "edit")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），配置档案只读，禁止回滚")
    # 回滚同样是向生产追加一个新版本，属于上线动作，窗口外一并拦截
    try:
        esvc.ensure_window_open(app_id, body.environment, actor_id=user["id"])
    except esvc.WindowClosedError as e:
        raise err(409, e.payload["message"], e.payload)
    try:
        result = svc.rollback(app_id, body.environment, body.version, user,
                              expected_version=body.expected_version)
    except svc.VersionConflictError as e:
        raise err(409, e.payload["message"], e.payload)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))
    # 回滚产生的是新版本；被回滚版本及之前的全部留痕原样保留
    return {"ok": True, **result}


# ---------------------------------------------------------------- 密文查看（服务端强制二次确认）

@router.post("/api/apps/{app_id}/config/reveal")
def reveal_secret(app_id: int, body: RevealIn, user: dict = User):
    app_row = get_app_or_404(app_id)
    reason = body.reason.strip()
    # 服务端强制：理由为空或过短直接拒绝，前端有没有拦都一样
    if len(reason) < REVEAL_REASON_MIN:
        raise err(400, f"查看密文明文必须填写不少于 {REVEAL_REASON_MIN} 个字的理由，服务端将记录本次查看")
    item = query_one(
        "SELECT * FROM config_items WHERE id = ? AND app_id = ?",
        (body.item_id, app_id),
    )
    if not item:
        raise err(404, f"配置项 #{body.item_id} 不存在")
    if not item["is_secret"]:
        raise err(400, "该配置项不是密文，无需查看明文")
    # 密文查看权单独校验：应用负责人/有编辑权不蕴涵明文权
    perms.ensure_config_perm(user, app_row, item["environment"], "reveal")
    svc.log_reveal(app_id, item["environment"], body.item_id, item["key"], user, reason)
    return {
        "item_id": item["id"],
        "key": item["key"],
        "value": item["value"],  # 仅在此接口、具备明文权且带理由时返回一次明文
        "revealed_at": int(time.time()),
    }


# ---------------------------------------------------------------- 环境对比

@router.get("/api/apps/{app_id}/config/diff")
def diff_config(app_id: int, env_a: str, env_b: str, user: dict = User):
    app_row = get_app_or_404(app_id)
    app_env_or_400(app_id, env_a)
    app_env_or_400(app_id, env_b)
    if env_a == env_b:
        raise err(400, "环境对比必须选择两个不同的环境")
    # 两个环境都得在可见范围内
    perms.ensure_config_perm(user, app_row, env_a, "view")
    perms.ensure_config_perm(user, app_row, env_b, "view")
    return svc.diff_environments(app_id, env_a, env_b)


# ---------------------------------------------------------------- 版本

@router.get("/api/apps/{app_id}/config/versions")
def get_versions(app_id: int, environment: str, user: dict = User):
    app_row = get_app_or_404(app_id)
    app_env_or_400(app_id, environment)
    perms.ensure_config_perm(user, app_row, environment, "view")
    return svc.list_versions(app_id, environment)


@router.get("/api/apps/{app_id}/config/versions/{version_no}")
def get_version(app_id: int, version_no: int, environment: str, user: dict = User):
    app_row = get_app_or_404(app_id)
    app_env_or_400(app_id, environment)
    perms.ensure_config_perm(user, app_row, environment, "view")
    try:
        return svc.version_detail(app_id, environment, version_no)
    except LookupError as e:
        raise err(404, str(e))


@router.get("/api/apps/{app_id}/config/rollback-preview")
def rollback_preview(app_id: int, environment: str, version: int, user: dict = User):
    app_row = get_app_or_404(app_id)
    app_env_or_400(app_id, environment)
    # 预览是读操作，有查看权即可看到差异（真正回滚时再卡编辑权）
    perms.ensure_config_perm(user, app_row, environment, "view")
    try:
        return svc.rollback_preview(app_id, environment, version)
    except LookupError as e:
        raise err(404, str(e))


# ---------------------------------------------------------------- 变更留痕

def _audit_rows(user: dict, app_id, business_line_id, environment, action, start, end):
    if action and action not in svc.ACTION_LABELS:
        raise err(400, f"非法动作类型：{action}")
    if environment:
        env_checked(environment)
    if app_id:
        # 按应用查询：应用本身要可见；若又显式指定了具体环境，该 应用×环境 也要在范围内
        app_row = query_one("SELECT * FROM applications WHERE id = ?", (app_id,))
        if not app_row:
            raise err(404, f"应用 #{app_id} 不存在")
        perms.ensure_app_visible(user, dict(app_row))
        if environment:
            perms.ensure_config_perm(user, dict(app_row), environment, "view")
    elif business_line_id:
        # 业务线级聚合查询：显式按范围外业务线 → 越权
        ensure_bl_visible(user, business_line_id)
        # 指定了具体环境却在该业务线该环境无任何授权 → 明确 403，而非空列表
        # （这里不认"拥有该线里某个应用"，聚合面按业务线×环境授权判定）
        if environment and not perms.has_bl_env_view(user, business_line_id, environment):
            raise err(403, perms.deny_reason(user, business_line_id, environment, "view"))
    try:
        sql, params = svc.query_audit(
            user, app_id=app_id, business_line_id=business_line_id,
            environment=environment, action=action, start=start, end=end,
        )
    except ValueError as e:
        raise err(400, str(e))
    return query(sql, tuple(params))


@router.get("/api/config/audit")
def list_audit(user: dict = User,
               app_id: int | None = None,
               business_line_id: int | None = None,
               environment: str | None = None,
               action: str | None = None,
               start: str | None = None,
               end: str | None = None):
    rows = _audit_rows(user, app_id, business_line_id, environment, action, start, end)
    return [svc.audit_row_to_dict(r) for r in rows]


@router.get("/api/config/audit/export.csv")
def export_audit(user: dict = User,
                 app_id: int | None = None,
                 business_line_id: int | None = None,
                 environment: str | None = None,
                 action: str | None = None,
                 start: str | None = None,
                 end: str | None = None):
    rows = _audit_rows(user, app_id, business_line_id, environment, action, start, end)
    csv_text = svc.export_csv(rows)
    filename = f"config-changelog-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
