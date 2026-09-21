"""织云系统 - 上游数据同步 API。

同步是平台级能力：设置、手动触发、冲突裁决、上游删除处理只开放给平台管理员；
运行报告、冲突/删除/留痕的查看同样仅管理员可见（同步跨全部业务线，不按个人范围收窄）。

- 定时按 sync_settings 间隔由后台调度器跑；手动触发立刻跑一趟；
- 每趟报告给出 新增/改动/无变化/冲突/未通过/本地已删拒收/上游已删待处理 计数与逐条原因；
- 冲突必须显式裁决（留本地 / 取上游 + 备注），裁决人与裁决结果进留痕；
- 上游删除必须显式处理（下线本地实体 / 确认忽略），不允许假装没看见。
"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import permissions as perms
from .. import sync_service as svc
from .. import sync_source
from ..auth import User, err

router = APIRouter()


def _require_admin(user: dict) -> None:
    if not perms.is_admin(user):
        raise err(403, "数据同步是平台级能力（跨全部业务线），仅平台管理员可操作与查看")


# ---------------------------------------------------------------- 请求模型

class SettingsIn(BaseModel):
    enabled: bool = False
    interval_seconds: int = Field(default=300, ge=30, le=86400)
    source_scene: str = "steady"


class TriggerIn(BaseModel):
    source_scene: str | None = None   # 手动试跑可临时指定剧本，不改动定时设置


class DecideIn(BaseModel):
    choice: str                        # keep_local / take_upstream
    note: str = Field(default="", max_length=200)


class RemoteDeleteIn(BaseModel):
    action: str                        # offline / ignore
    note: str = Field(default="", max_length=200)


# ---------------------------------------------------------------- 总览 / 设置 / 触发

@router.get("/api/sync/status")
def sync_status(user: dict = User):
    _require_admin(user)
    return svc.status_overview()


@router.put("/api/sync/settings")
def put_settings(body: SettingsIn, user: dict = User):
    _require_admin(user)
    try:
        s = svc.update_settings(body.enabled, body.interval_seconds, body.source_scene, user)
    except ValueError as e:
        raise err(400, str(e))
    return {"ok": True, "settings": {
        "enabled": bool(s["enabled"]), "interval_seconds": s["interval_seconds"],
        "source_scene": s["source_scene"],
        "scene_label": sync_source.SCENE_LABELS.get(s["source_scene"], s["source_scene"])}}


@router.post("/api/sync/run")
def trigger_run(body: TriggerIn = TriggerIn(), user: dict = User):
    _require_admin(user)
    try:
        result = svc.run_sync("manual", user["id"], body.source_scene)
    except svc.SyncRunningError as e:
        raise err(409, str(e), {"code": "sync_running"})
    except ValueError as e:
        raise err(400, str(e))
    if result.get("status") == "failed":
        raise err(502, result.get("error", "同步失败"), {"code": "sync_failed", **result})
    return result


@router.post("/api/sync/demo/reset")
def reset_demo(user: dict = User):
    _require_admin(user)
    sync_source.reset_demo()
    svc.log_audit(user["id"], "setting", "", "", "重置同步演示数据（清空同步实体与全部队列）", None)
    return {"ok": True}


# ---------------------------------------------------------------- 运行报告

@router.get("/api/sync/runs")
def list_runs(user: dict = User, limit: int = 30):
    _require_admin(user)
    return svc.list_runs(limit)


@router.get("/api/sync/runs/{run_id}")
def get_run(run_id: int, user: dict = User):
    _require_admin(user)
    d = svc.get_run(run_id)
    if d is None:
        raise err(404, f"同步运行 #{run_id} 不存在")
    return d


# ---------------------------------------------------------------- 冲突裁决

@router.get("/api/sync/conflicts")
def list_conflicts(user: dict = User, status: str = "pending"):
    _require_admin(user)
    if status not in ("pending", "keep_local", "take_upstream", "all"):
        raise err(400, "status 仅支持 pending/keep_local/take_upstream/all")
    if status == "all":
        rows = []
        for st in ("pending", "keep_local", "take_upstream"):
            rows.extend(svc.list_conflicts(st))
        return rows
    return svc.list_conflicts(status)


@router.get("/api/sync/conflicts/{conflict_id}")
def get_conflict(conflict_id: int, user: dict = User):
    _require_admin(user)
    row = svc.get_conflict(conflict_id)
    if row is None:
        raise err(404, "冲突记录不存在")
    d = svc.conflict_detail(row)
    decider = None
    if row["decided_by"]:
        from ..db import query_one
        u = query_one("SELECT name FROM users WHERE id=?", (row["decided_by"],))
        decider = u["name"] if u else None
    d["decided_by_name"] = decider
    return d


@router.post("/api/sync/conflicts/{conflict_id}/decide")
def decide_conflict(conflict_id: int, body: DecideIn, user: dict = User):
    _require_admin(user)
    try:
        return svc.decide_conflict(conflict_id, body.choice, body.note, user)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))


# ---------------------------------------------------------------- 上游删除 / 本地删除 / 墓碑

@router.get("/api/sync/remote-deletions")
def list_remote_deletions(user: dict = User, status: str | None = None):
    _require_admin(user)
    if status and status not in ("pending", "offlined", "ignored"):
        raise err(400, "status 仅支持 pending/offlined/ignored")
    return svc.list_remote_deletions(status)


@router.post("/api/sync/remote-deletions/{deletion_id}/handle")
def handle_remote_deletion(deletion_id: int, body: RemoteDeleteIn, user: dict = User):
    _require_admin(user)
    try:
        return svc.handle_remote_deletion(deletion_id, body.action, body.note, user)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))


@router.get("/api/sync/local-entities")
def list_local_synced(user: dict = User):
    _require_admin(user)
    return svc.list_local_synced()


class LocalDeleteIn(BaseModel):
    entity_type: str
    local_id: int
    note: str = Field(default="", max_length=200)


@router.post("/api/sync/local-entities/delete")
def delete_local_synced(body: LocalDeleteIn, user: dict = User):
    _require_admin(user)
    if body.entity_type not in ("app", "module", "environment"):
        raise err(400, "实体类型仅支持 app/module/environment")
    try:
        svc.delete_local_synced(body.entity_type, body.local_id, user)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))
    return {"ok": True}


@router.get("/api/sync/tombstones")
def list_tombstones(user: dict = User):
    _require_admin(user)
    return svc.list_tombstones()


# ---------------------------------------------------------------- 留痕

@router.get("/api/sync/audit")
def sync_audit(user: dict = User, limit: int = 100):
    _require_admin(user)
    return svc.list_audit(limit)
