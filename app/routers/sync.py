"""织云系统 - 上游数据同步 API。

- 调度：GET/PUT /api/sync/settings，POST /api/sync/run（手动立刻跑一趟）；
- 结果：/api/sync/status（最近一趟汇总）、/api/sync/runs（历史）、/api/sync/runs/{id}（逐对象明细）；
- 待裁决：/api/sync/conflicts + /decide（双方同改 / 上游删除 / 本地已删三类）；
- 模拟源：/api/sync/source/reset（重新对齐）、/api/sync/source/advance（演进一幕）；
- 模块本地 CRUD：/api/apps/{id}/modules；
- 删除应用：DELETE /api/apps/{id}（级联删除并写同步墓碑，上游再推不复活）。

同步是平台级动作，调度设置/模拟源控制仅平台管理员；裁决与删除按应用管理权限收口
（管理员、本业务线负责人、应用负责人；只读观察者封顶）。
"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import env_service as esvc
from .. import permissions as perms
from .. import sync_service as sync
from .. import sync_source as source
from ..auth import User, err, get_app_or_404, get_app_writable
from ..db import query_one

router = APIRouter()


# ---------------------------------------------------------------- 请求模型

class SyncSettingsIn(BaseModel):
    enabled: bool
    interval_seconds: int = Field(ge=30, le=86400)


class DecideIn(BaseModel):
    resolution: str
    note: str = Field(default="", max_length=200)


class ModuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    module_type: str = "service"
    version_tag: str = Field(default="", max_length=64)
    status: str = "active"
    description: str = Field(default="", max_length=500)


class DeleteAppIn(BaseModel):
    confirm: bool = False
    note: str = Field(default="", max_length=200)


# ---------------------------------------------------------------- 权限

def _ensure_admin(user: dict) -> None:
    if not perms.is_admin(user):
        raise err(403, "同步调度与模拟源控制仅限平台管理员操作")


def _ensure_decide_perm(user: dict, conflict: dict) -> None:
    if perms.is_admin(user):
        return
    if user["role"] == "viewer":
        raise err(403, "只读观察者不能裁决同步差异")
    app_id = conflict.get("app_id")
    if not app_id:
        raise err(403, "该差异对应的应用已不在本地或归属无法判定，仅限平台管理员裁决")
    app_row = query_one("SELECT business_line_id FROM applications WHERE id=?", (app_id,))
    if not app_row:
        raise err(403, "该差异对应的应用已不存在，仅限平台管理员裁决")
    if perms.is_bl_owner(user, app_row["business_line_id"]):
        return
    if app_id in user.get("_owned_apps", set()):
        return
    raise err(403, "你不是该应用的负责人或其所属业务线负责人，不能裁决这条同步差异")


# ---------------------------------------------------------------- 状态 / 调度 / 触发

@router.get("/api/sync/status")
def sync_status(user: dict = User):
    return sync.status_overview()


@router.put("/api/sync/settings")
def update_sync_settings(body: SyncSettingsIn, user: dict = User):
    _ensure_admin(user)
    try:
        settings = sync.update_settings(body.enabled, body.interval_seconds, user)
    except ValueError as e:
        raise err(400, str(e))
    return {"ok": True, "settings": settings}


@router.post("/api/sync/run", status_code=201)
def trigger_sync(user: dict = User):
    # 手动触发：管理员与业务线负责人、应用负责人都可以，只读观察者不允许。
    if user["role"] == "viewer":
        raise err(403, "只读观察者不能手动触发同步")
    try:
        return sync.run_sync("manual", user)
    except sync.SyncRunningError as e:
        raise err(409, str(e))


# ---------------------------------------------------------------- 趟次记录

@router.get("/api/sync/runs")
def list_sync_runs(user: dict = User, limit: int = 20):
    limit = max(1, min(int(limit or 20), 100))
    return sync.list_runs(limit)


@router.get("/api/sync/runs/{run_id}")
def get_sync_run(run_id: int, user: dict = User):
    try:
        return sync.get_run(run_id)
    except LookupError as e:
        raise err(404, str(e))


# ---------------------------------------------------------------- 冲突 / 裁决

@router.get("/api/sync/conflicts")
def list_conflicts(user: dict = User, status: str = "pending"):
    if status not in ("pending", "resolved", "all"):
        raise err(400, "status 仅支持 pending/resolved/all")
    rows = sync.list_conflicts(status)
    if perms.is_admin(user):
        return rows
    # 非管理员按应用归属/业务线收窄；应用已删除的本地删除类差异只给管理员看
    app_ids = sorted({c["app_id"] for c in rows if c["app_id"]})
    bl_of: dict[int, int] = {}
    if app_ids:
        from ..db import query
        placeholders = ",".join("?" * len(app_ids))
        bl_of = {r["id"]: r["business_line_id"] for r in query(
            f"SELECT id, business_line_id FROM applications WHERE id IN ({placeholders})",
            tuple(app_ids))}
    visible = []
    for c in rows:
        app_id = c["app_id"]
        if not app_id:
            continue
        if perms.is_bl_owner(user, bl_of.get(app_id)) or app_id in user.get("_owned_apps", set()):
            visible.append(c)
    return visible


@router.post("/api/sync/conflicts/{conflict_id}/decide")
def decide_conflict(conflict_id: int, body: DecideIn, user: dict = User):
    c = query_one("SELECT * FROM sync_conflicts WHERE id=?", (conflict_id,))
    if not c:
        raise err(404, f"待裁决事项 #{conflict_id} 不存在")
    _ensure_decide_perm(user, dict(c))
    try:
        return sync.decide_conflict(conflict_id, body.resolution, body.note, user)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))
    except esvc.EnvDeleteBlockedError as e:
        # 跟随删除时仍有挂载（409，携带挂载清单）
        raise err(409, e.payload["message"], e.payload)


# ---------------------------------------------------------------- 模拟同步源（演示）

@router.post("/api/sync/source/reset")
def reset_source(user: dict = User):
    _ensure_admin(user)
    return {"ok": True, **source.reset_source(user["id"])}


@router.post("/api/sync/source/advance")
def advance_source(user: dict = User):
    _ensure_admin(user)
    return {"ok": True, **source.advance_scenario(user["id"])}


# ---------------------------------------------------------------- 模块管理

@router.get("/api/apps/{app_id}/modules")
def list_modules(app_id: int, user: dict = User):
    get_app_or_404(app_id)
    perms.ensure_app_visible(user, get_app_or_404(app_id))
    return sync.list_modules(app_id)


@router.post("/api/apps/{app_id}/modules", status_code=201)
def create_module(app_id: int, body: ModuleIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "模块管理")
    try:
        return sync.create_module(app_id, body.model_dump(), user)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))


@router.put("/api/apps/{app_id}/modules/{module_id}")
def update_module(app_id: int, module_id: int, body: ModuleIn, user: dict = User):
    get_app_writable(user, app_id, "模块管理")
    row = query_one("SELECT * FROM app_modules WHERE id=? AND app_id=?", (module_id, app_id))
    if not row:
        raise err(404, f"模块 #{module_id} 不存在或不属于该应用")
    try:
        return sync.update_module(module_id, body.model_dump(), user)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))


@router.delete("/api/apps/{app_id}/modules/{module_id}")
def delete_module(app_id: int, module_id: int, user: dict = User):
    get_app_writable(user, app_id, "模块管理")
    row = query_one("SELECT id FROM app_modules WHERE id=? AND app_id=?", (module_id, app_id))
    if not row:
        raise err(404, f"模块 #{module_id} 不存在或不属于该应用")
    sync.delete_module(module_id, user)
    return {"ok": True}


# ---------------------------------------------------------------- 删除应用（写墓碑 + 级联）

@router.delete("/api/apps/{app_id}")
def delete_app(app_id: int, body: DeleteAppIn | None = None, user: dict = User):
    app_row = get_app_or_404(app_id)
    # 删除整个应用属于业务线级动作：管理员 / 本业务线负责人；应用负责人不开放
    if not (perms.is_admin(user) or perms.is_bl_owner(user, app_row["business_line_id"])):
        raise err(403, "删除应用仅限平台管理员或该应用所属业务线的负责人")
    body = body or DeleteAppIn()
    try:
        return sync.delete_app(app_id, user, note=body.note, confirm=body.confirm)
    except LookupError as e:
        raise err(404, str(e))
    except Exception as e:
        # EnvDeleteBlockedError 携带挂载清单（409）
        payload = getattr(e, "payload", None)
        if payload:
            raise err(409, payload["message"], payload)
        raise err(400, str(e))
