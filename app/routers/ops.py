"""织云系统 - 环境管理 / 发布窗口 / 应用健康 API。

- 环境是每应用下的注册表：业务线可自定义增删；删除前清点挂载（配置/版本/实例/所属环境），
  有挂载一律 409 并说明挂在哪里；
- 发布窗口：周几 × 时段 + 节假日封网；窗口外的配置上线在 config 路由统一被 409 拦截，
  响应里说清原因与"下一次能发是什么时候"；
- 实例健康：登记/移除实例，模拟监控的重启/掉线/恢复，24h/7d 重启频率分级；
- 作战台 /api/console/health-alerts 直接冒出掉线实例（带业务线×环境）与频繁重启环境；
- 窗口与健康的所有改动写 ops_audit_logs，可按应用/业务线/环境/类别/时间窗查询与导出。
"""
import time
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import env_service as esvc
from .. import permissions as perms
from ..auth import User, ensure_bl_visible, err, get_app_or_404, get_app_writable
from ..db import TERMINAL_STATUS, query, query_one

router = APIRouter()


# ---------------------------------------------------------------- 请求模型

class EnvCreateIn(BaseModel):
    env_key: str = Field(min_length=1, max_length=32)
    env_label: str = Field(min_length=1, max_length=16)


class WindowIn(BaseModel):
    deploy_restricted: bool = True
    window_days: list[int] = []
    window_start: str = "10:00"
    window_end: str = "18:00"


class HolidayIn(BaseModel):
    date: str
    reason: str = Field(default="", max_length=100)


class InstanceIn(BaseModel):
    environment: str
    name: str = Field(min_length=1, max_length=64)


class InstanceActionIn(BaseModel):
    detail: str = Field(default="", max_length=200)


# ---------------------------------------------------------------- 辅助

def _env_visible(user: dict, app_row: dict, env_key: str) -> bool:
    if perms.is_admin(user) or perms.is_bl_owner(user, app_row["business_line_id"]):
        return True
    if app_row["id"] in user.get("_owned_apps", set()):
        return True
    return bool(perms._grant_match(perms.grants_of(user),
                                   app_row["business_line_id"], env_key, "view"))


def _ensure_env_visible(user: dict, app_row: dict, env_key: str) -> None:
    if not _env_visible(user, app_row, env_key):
        raise err(403, perms.deny_reason(user, app_row["business_line_id"], env_key, "view"))


def _not_offline(app_row: dict) -> None:
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），环境、发布窗口与实例均为只读，禁止变更")


def _env_row_or_404(app_id: int, env_id: int) -> dict:
    row = query_one("SELECT * FROM app_environments WHERE app_id=? AND id=?", (app_id, env_id))
    if not row:
        raise err(404, f"环境 #{env_id} 不存在或已被删除")
    return dict(row)


def _instance_or_404(instance_id: int):
    row = query_one("SELECT * FROM app_instances WHERE id=?", (instance_id,))
    if not row:
        raise err(404, f"实例 #{instance_id} 不存在或已被移除")
    return row


# ---------------------------------------------------------------- 环境列表 / 新增

@router.get("/api/apps/{app_id}/environments")
def list_app_environments(app_id: int, user: dict = User):
    app_row = get_app_or_404(app_id)
    # 应用整体不可见直接 403；列表内部再按各环境授权收窄
    perms.ensure_app_visible(user, app_row)
    envs = esvc.list_environments(app_id, app_row)
    can_manage = perms.can_manage_app(user, app_row) and app_row["status"] != TERMINAL_STATUS
    out = []
    for e in envs:
        visible = _env_visible(user, app_row, e["env_key"])
        e["visible"] = visible
        e["can_manage"] = bool(can_manage)
        if visible:
            out.append(e)
        elif perms.is_admin(user) or perms.is_bl_owner(user, app_row["business_line_id"]):
            out.append(e)
    # 应用负责人对本人应用拥有全部环境可见性，上面已放行；其余无授权环境直接不出现
    return out


@router.post("/api/apps/{app_id}/environments", status_code=201)
def create_app_environment(app_id: int, body: EnvCreateIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "环境管理")
    _not_offline(app_row)
    try:
        env = esvc.create_environment(app_id, app_row, body.env_key, body.env_label, user)
    except ValueError as e:
        raise err(400, str(e))
    env["visible"] = True
    env["can_manage"] = True
    return env


# ---------------------------------------------------------------- 发布窗口 / 节假日 / 删除

@router.put("/api/apps/{app_id}/environments/{env_id}/window")
def update_window(app_id: int, env_id: int, body: WindowIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "发布窗口")
    _not_offline(app_row)
    _env_row_or_404(app_id, env_id)
    try:
        return esvc.update_window(app_id, env_id, app_row, user,
                                  body.deploy_restricted, body.window_days,
                                  body.window_start, body.window_end)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))


@router.post("/api/apps/{app_id}/environments/{env_id}/holidays", status_code=201)
def add_holiday(app_id: int, env_id: int, body: HolidayIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "节假日封网")
    _not_offline(app_row)
    _env_row_or_404(app_id, env_id)
    try:
        return esvc.add_holiday(app_id, env_id, app_row, user, body.date, body.reason)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))


@router.delete("/api/apps/{app_id}/environments/{env_id}/holidays/{holiday_id}")
def remove_holiday(app_id: int, env_id: int, holiday_id: int, user: dict = User):
    app_row = get_app_writable(user, app_id, "节假日封网")
    _not_offline(app_row)
    _env_row_or_404(app_id, env_id)
    try:
        esvc.remove_holiday(app_id, env_id, holiday_id, app_row, user)
    except LookupError as e:
        raise err(404, str(e))
    return {"ok": True}


@router.delete("/api/apps/{app_id}/environments/{env_id}")
def delete_environment(app_id: int, env_id: int, user: dict = User):
    app_row = get_app_writable(user, app_id, "环境管理")
    _not_offline(app_row)
    _env_row_or_404(app_id, env_id)
    try:
        esvc.delete_environment(app_id, env_id, app_row, user)
    except LookupError as e:
        raise err(404, str(e))
    except esvc.EnvDeleteBlockedError as e:
        raise err(409, e.payload["message"], e.payload)
    return {"ok": True}


@router.get("/api/apps/{app_id}/environments/{env_id}/window")
def get_window(app_id: int, env_id: int, user: dict = User):
    """单独取某环境窗口状态（含下一次开放时间），供配置页提示条使用。"""
    app_row = get_app_or_404(app_id)
    env = _env_row_or_404(app_id, env_id)
    _ensure_env_visible(user, app_row, env["env_key"])
    return esvc.env_to_dict(env, app_row)


# ---------------------------------------------------------------- 实例健康

@router.get("/api/apps/{app_id}/health")
def app_health(app_id: int, environment: str | None = None, user: dict = User):
    app_row = get_app_or_404(app_id)
    perms.ensure_app_visible(user, app_row)
    if environment:
        # 显式指定环境：环境不存在于该应用 → 400；存在但不在可见范围 → 403（不静默给空）
        env_row = query_one(
            "SELECT * FROM app_environments WHERE app_id=? AND env_key=?",
            (app_id, environment),
        )
        if not env_row:
            raise err(400, f"该应用下不存在环境「{environment}」，请先在环境管理中新增")
        _ensure_env_visible(user, app_row, environment)
    env_rows = query("SELECT * FROM app_environments WHERE app_id=? ORDER BY id", (app_id,))
    result = []
    for r in env_rows:
        if environment and r["env_key"] != environment:
            continue
        if not _env_visible(user, app_row, r["env_key"]):
            continue
        d = esvc.env_to_dict(r, app_row)
        result.append({
            "env_id": r["id"], "environment": r["env_key"], "env_label": r["env_label"],
            "is_builtin": bool(r["is_builtin"]),
            "window_open_now": d["window_open_now"], "window_text": d["window_text"],
            "window_status": d["window_status"],
            "health": d["health"],
        })
    return {"app_id": app_id, "app_name": app_row["name"], "environments": result}


@router.post("/api/apps/{app_id}/instances", status_code=201)
def register_instance(app_id: int, body: InstanceIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "实例管理")
    _not_offline(app_row)
    if not _env_visible(user, app_row, body.environment):
        raise err(403, "该环境不在你的可见范围内，不能在其下登记实例")
    try:
        return esvc.register_instance(app_id, body.environment, body.name, user)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))


@router.delete("/api/apps/{app_id}/instances/{instance_id}")
def remove_instance(app_id: int, instance_id: int, user: dict = User):
    app_row = get_app_writable(user, app_id, "实例管理")
    _not_offline(app_row)
    inst = _instance_or_404(instance_id)
    if inst["app_id"] != app_id:
        raise err(404, f"实例 #{instance_id} 不属于该应用")
    esvc.remove_instance(instance_id, user)
    return {"ok": True}


def _instance_action(app_id: int, instance_id: int, body: InstanceActionIn,
                     user: dict, fn, what: str):
    app_row = get_app_writable(user, app_id, "实例管理")
    _not_offline(app_row)
    inst = _instance_or_404(instance_id)
    if inst["app_id"] != app_id:
        raise err(404, f"实例 #{instance_id} 不属于该应用")
    try:
        return fn(instance_id, user, body.detail.strip())
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))


@router.post("/api/apps/{app_id}/instances/{instance_id}/restart", status_code=201)
def restart_instance(app_id: int, instance_id: int, body: InstanceActionIn = InstanceActionIn(),
                     user: dict = User):
    return _instance_action(app_id, instance_id, body, user, esvc.restart_instance, "重启")


@router.post("/api/apps/{app_id}/instances/{instance_id}/offline", status_code=201)
def mark_offline(app_id: int, instance_id: int, body: InstanceActionIn = InstanceActionIn(),
                 user: dict = User):
    return _instance_action(app_id, instance_id, body, user, esvc.mark_offline, "掉线")


@router.post("/api/apps/{app_id}/instances/{instance_id}/recover", status_code=201)
def mark_recover(app_id: int, instance_id: int, body: InstanceActionIn = InstanceActionIn(),
                 user: dict = User):
    return _instance_action(app_id, instance_id, body, user, esvc.mark_recover, "恢复")


@router.get("/api/apps/{app_id}/instances/{instance_id}/events")
def instance_events(app_id: int, instance_id: int, user: dict = User):
    app_row = get_app_or_404(app_id)
    perms.ensure_app_visible(user, app_row)
    inst = _instance_or_404(instance_id)
    if inst["app_id"] != app_id:
        raise err(404, f"实例 #{instance_id} 不属于该应用")
    _ensure_env_visible(user, app_row, inst["environment"])
    return esvc.list_instance_events(instance_id)


# ---------------------------------------------------------------- 作战台与总览

@router.get("/api/health/overview")
def health_overview(user: dict = User, business_line_id: int | None = None,
                    only_alerts: bool = False):
    """跨应用环境健康总览（按可见范围收窄）；only_alerts 只回掉线/重启异常环境。"""
    if business_line_id:
        ensure_bl_visible(user, business_line_id)
    cards = esvc.all_env_health(user)
    if business_line_id:
        cards = [c for c in cards if c["business_line_id"] == business_line_id]
    if only_alerts:
        cards = [c for c in cards if c["health"]["level"] in ("offline", "critical", "warning")]
    return cards


@router.get("/api/console/health-alerts")
def health_alerts(user: dict = User):
    """资产作战台健康告警：掉线实例逐条冒出 + 频繁重启环境分组。"""
    return esvc.console_alerts(user)


# ---------------------------------------------------------------- 环境与健康留痕

def _ops_rows(user, app_id, business_line_id, environment, category, start, end):
    if environment:
        # 环境键不再有全局写死枚举，只做基本形态校验
        import re as _re
        if not _re.fullmatch(r"[a-z0-9_*-]{1,32}", environment):
            raise err(400, f"非法环境：{environment}")
    if app_id:
        app_row = query_one("SELECT * FROM applications WHERE id=?", (app_id,))
        if not app_row:
            raise err(404, f"应用 #{app_id} 不存在")
        perms.ensure_app_visible(user, dict(app_row))
        if environment:
            _ensure_env_visible(user, dict(app_row), environment)
    elif business_line_id:
        ensure_bl_visible(user, business_line_id)
        if environment and not perms.has_bl_env_view(user, business_line_id, environment):
            raise err(403, perms.deny_reason(user, business_line_id, environment, "view"))
    try:
        sql, params = esvc.query_ops_audit(
            user, app_id=app_id, business_line_id=business_line_id,
            environment=environment, category=category, start=start, end=end)
    except ValueError as e:
        raise err(400, str(e))
    return query(sql, tuple(params))


@router.get("/api/ops/audit")
def list_ops_audit(user: dict = User, app_id: int | None = None,
                   business_line_id: int | None = None, environment: str | None = None,
                   category: str | None = None, start: str | None = None,
                   end: str | None = None):
    rows = _ops_rows(user, app_id, business_line_id, environment, category, start, end)
    return [esvc.ops_row_to_dict(r) for r in rows]


@router.get("/api/ops/audit/meta")
def ops_audit_meta(user: dict = User):
    return {"categories": [{"value": k, "label": v} for k, v in esvc.CATEGORY_LABELS.items()]}


@router.get("/api/ops/audit/export.csv")
def export_ops_audit(user: dict = User, app_id: int | None = None,
                     business_line_id: int | None = None, environment: str | None = None,
                     category: str | None = None, start: str | None = None,
                     end: str | None = None):
    rows = _ops_rows(user, app_id, business_line_id, environment, category, start, end)
    csv_text = esvc.export_ops_csv(rows)
    filename = f"ops-changelog-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
