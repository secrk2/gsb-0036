"""织云系统 - 环境管理 / 发布窗口 / 应用健康 领域服务。

三块能力：
1. 每应用自定义环境（app_environments）+ 发布窗口（周几 × 时段 × 节假日封网）；
   窗口判定纯函数化，给"现在能不能发"结论的同时必须给出"下一次什么时候能发"，
   删除环境前清点挂在它上面的配置、版本与实例，有挂载一律拒绝并说明挂在哪里。
2. 每环境实例健康：存活/掉线、重启次数、最后重启时间、24 小时/7 天重启频率分级，
   把"一天重启二十次"与"半个月重启一次"明确区分开。
3. 环境与健康变更全部写 ops_audit_logs，可按应用/业务线/环境/类别/时间窗查询。
"""
import csv
import io
import json
import re
import time
from datetime import datetime, timedelta

from .db import (
    BUILTIN_ENV_KEYS, DEFAULT_WINDOWS, ENV_LABELS, ENV_KEY_PATTERN,
    get_conn, query, query_one,
)

WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 重启频率分级阈值（24 小时 / 7 天窗口）
CRASH_24H_CRITICAL = 5    # 24h ≥5 次：频繁重启（红色）
CRASH_24H_WARNING = 2     # 24h ≥2 次：重启偏多（黄色）
CRASH_7D_WARNING = 7      # 7 天 ≥7 次：重启偏多

LEVEL_LABELS = {
    "offline": "已掉线",
    "critical": "频繁重启",
    "warning": "重启偏多",
    "normal": "正常",
}

CATEGORY_LABELS = {
    "env_create": "新增环境",
    "env_delete": "删除环境",
    "window_update": "发布窗口调整",
    "holiday_add": "节假日封网",
    "holiday_remove": "解除封网",
    "instance_register": "登记实例",
    "instance_remove": "移除实例",
    "instance_restart": "实例重启",
    "instance_offline": "实例掉线",
    "instance_recover": "实例恢复",
    "deploy_block": "窗口外拦截上线",
}


class WindowClosedError(Exception):
    """不在发布窗口：payload 携带人话原因与下一次开放时间，路由层转 409。"""

    def __init__(self, payload: dict):
        super().__init__(payload["message"])
        self.payload = payload


class EnvDeleteBlockedError(Exception):
    """环境上还挂着配置/实例等，禁止删除；payload 给出逐项挂载清单。"""

    def __init__(self, payload: dict):
        super().__init__(payload["message"])
        self.payload = payload


# ---------------------------------------------------------------- 小工具

def env_key_ok(key: str) -> bool:
    return bool(re.fullmatch(ENV_KEY_PATTERN, key or ""))


def hhmm_ok(value: str) -> bool:
    return bool(HHMM.fullmatch(value or ""))


def _weekdays_text(days: list[int]) -> str:
    return "、".join(WEEK_CN[d] for d in sorted(days)) if days else "（未设置发布日）"


def _date_text(d: datetime) -> str:
    return f"{d.month}月{d.day}日（{WEEK_CN[d.weekday()]}）"


def _next_open_text(d: datetime, start: str) -> str:
    today = datetime.now().date()
    delta = (d.date() - today).days
    if delta == 0:
        prefix = "今天"
    elif delta == 1:
        prefix = "明天"
    elif delta == 2:
        prefix = "后天"
    else:
        prefix = _date_text(d)
    return f"{prefix} {start}"


def window_text(row, holiday_dates: set[str] | None = None) -> str:
    """发布窗口的一句话描述，用于列表、提示条、拦截原因。"""
    if not row["deploy_restricted"]:
        return "全时段允许发布"
    days = json.loads(row["window_days"] or "[]")
    base = f"每周 {_weekdays_text(days)} {row['window_start']}–{row['window_end']}"
    holidays = sorted(holiday_dates or [])
    if holidays:
        upcoming = "、".join(holidays[:3])
        more = f" 等 {len(holidays)} 天" if len(holidays) > 3 else ""
        return base + f"；节假日封网（{upcoming}{more}）"
    return base + "；节假日默认封网"


def _load_holidays(env_id: int, within_from: datetime, within_to: datetime) -> set[str]:
    rows = query(
        "SELECT holiday_date FROM env_holidays WHERE env_id=? AND holiday_date BETWEEN ? AND ?",
        (env_id, within_from.strftime("%Y-%m-%d"), within_to.strftime("%Y-%m-%d")),
    )
    return {r["holiday_date"] for r in rows}


# ---------------------------------------------------------------- 发布窗口判定

def evaluate_window(row, at: int | None = None) -> dict:
    """判定某时刻是否处于发布窗口。

    返回 allowed / 原因 / 下一次开放时间戳与人话；不允许时原因必须具体到
    "今天封网"、"今天不是发布日"、"窗口已于 18:00 关闭"等，而不是只给一个灰按钮。
    """
    now_ts = int(at if at is not None else time.time())
    now = datetime.fromtimestamp(now_ts)
    unrestricted = not row["deploy_restricted"]
    holidays = set()
    if not unrestricted:
        holidays = _load_holidays(row["id"], now, now + timedelta(days=31))
    wtext = window_text(row, {h for h in holidays if h >= now.strftime("%Y-%m-%d")})

    if unrestricted:
        return {
            "allowed": True, "restricted": False, "reason_code": "unrestricted",
            "message": "该环境全时段允许发布。", "window_text": wtext,
            "next_open_at": None, "next_close_at": None,
        }

    days = sorted(json.loads(row["window_days"] or "[]"))
    start_h, start_m = map(int, row["window_start"].split(":"))
    end_h, end_m = map(int, row["window_end"].split(":"))
    today_str = now.strftime("%Y-%m-%d")
    today_holiday = today_str in holidays
    holiday_reason = None
    if today_holiday:
        hrow = query_one("SELECT reason FROM env_holidays WHERE env_id=? AND holiday_date=?",
                         (row["id"], today_str))
        holiday_reason = (hrow["reason"] if hrow and hrow["reason"] else "") or "节假日封网"

    def open_at(d: datetime) -> int:
        return int(time.mktime(d.replace(hour=start_h, minute=start_m, second=0).timetuple()))

    def close_at(d: datetime) -> int:
        return int(time.mktime(d.replace(hour=end_h, minute=end_m, second=0).timetuple()))

    # 今天本身是发布日且未封网：按当天时段判定
    if now.weekday() in days and not today_holiday:
        opens = open_at(now)
        closes = close_at(now)
        if now_ts < opens:
            return _closed("before_window",
                           f"当前不在发布时段：今天 {WEEK_CN[now.weekday()]} 的窗口 {row['window_start']} 才开放。",
                           wtext, opens, holidays=holidays, today_holiday=holiday_reason)
        if now_ts <= closes:
            return {
                "allowed": True, "restricted": True, "reason_code": "in_window",
                "message": f"正处于发布窗口（截至今天 {row['window_end']}）。",
                "window_text": wtext, "next_open_at": None, "next_close_at": closes,
            }
        # 今天窗口已过，落到向后扫描

    # 向后最多扫 31 天：找第一个"发布日且不封网"的日期
    candidate = now + timedelta(days=1)
    next_open_ts = None
    blocked_by_holiday = None
    for _ in range(31):
        ds = candidate.strftime("%Y-%m-%d")
        if candidate.weekday() in days and ds not in holidays:
            next_open_ts = open_at(candidate)
            break
        if candidate.weekday() in days and ds in holidays:
            blocked_by_holiday = ds
        candidate += timedelta(days=1)

    if not days:
        reason_code, msg = "no_window_days", "该环境尚未配置任何发布日，当前完全禁止发布，请先设置发布窗口。"
    elif today_holiday:
        reason_code = "holiday"
        msg = f"今天是节假日封网日（{holiday_reason}），不允许发布。"
    elif now.weekday() in days:
        reason_code = "window_passed"
        msg = f"今天的发布窗口已于 {row['window_end']} 关闭。"
    else:
        reason_code = "not_allowed_day"
        msg = f"今天是 {WEEK_CN[now.weekday()]}，不在发布日（{_weekdays_text(days)}）。"

    return _closed(reason_code, msg, wtext, next_open_ts, holidays=holidays,
                   today_holiday=holiday_reason, blocked_by_holiday=blocked_by_holiday)


def _closed(reason_code, message, wtext, next_open_ts, *, holidays=None,
            today_holiday=None, blocked_by_holiday=None) -> dict:
    next_text = None
    if next_open_ts:
        d = datetime.fromtimestamp(next_open_ts)
        next_text = _next_open_text(d, d.strftime("%H:%M"))
        if blocked_by_holiday:
            bd = datetime.strptime(blocked_by_holiday, "%Y-%m-%d")
            message += f" 注意 {_date_text(bd)} 为封网日，已跳过。"
        message += f" 下一次开放发布：{next_text}。"
    else:
        message += " 未来 31 天内没有可发布的时间，请检查发布窗口与节假日封网安排。"
    return {
        "allowed": False, "restricted": True, "reason_code": reason_code,
        "message": message, "window_text": wtext,
        "next_open_at": next_open_ts, "next_open_label": next_text,
        "next_close_at": None, "today_holiday": today_holiday,
    }


def ensure_window_open(app_id: int, env_key: str, at: int | None = None,
                       actor_id: int | None = None) -> dict:
    """上线动作的统一卡口：不在窗口直接抛 WindowClosedError（路由转 409）。

    拦截本身也写一条 deploy_block 留痕：谁在窗口外尝试上线、被拦的是哪个环境，
    与窗口调整留痕放在一起可审计。
    """
    row = query_one(
        "SELECT * FROM app_environments WHERE app_id=? AND env_key=?",
        (app_id, env_key),
    )
    if not row:
        # 未注册环境（旧数据/异常）按全时段放行，但不留窗口口子给非法环境键
        return {"allowed": True, "restricted": False, "window_text": "未配置发布窗口",
                "next_open_at": None}
    result = evaluate_window(row, at=at)
    if not result["allowed"]:
        result.update({
            "code": "deploy_window_closed",
            "environment": env_key,
            "environment_label": row["env_label"],
        })
        log_ops(app_id, env_key, row["env_label"], "deploy_block", row["env_label"],
                f"窗口外尝试上线被拦截：{result['message']}", actor_id)
        raise WindowClosedError(result)
    return result


# ---------------------------------------------------------------- 环境注册

def ensure_default_environments(app_id: int, created_at: int | None = None) -> None:
    """新建应用后注册四个标准环境（幂等），默认窗口随环境类型。"""
    now = int(time.time())
    ts = created_at or now
    for key in BUILTIN_ENV_KEYS:
        exists = query_one(
            "SELECT 1 FROM app_environments WHERE app_id=? AND env_key=?", (app_id, key)
        )
        if exists:
            continue
        w = DEFAULT_WINDOWS[key]
        get_conn().execute(
            """INSERT INTO app_environments
               (app_id, env_key, env_label, is_builtin, deploy_restricted,
                window_days, window_start, window_end, created_by, created_at, updated_at)
               VALUES (?,?,?,1,?,?,?,?,NULL,?,?)""",
            (app_id, key, ENV_LABELS[key], w["restricted"],
             json.dumps(w["days"]), w["start"], w["end"], ts, now),
        )
    get_conn().commit()


def get_env_row(app_id: int, env_id: int | None = None, env_key: str | None = None):
    if env_id is not None:
        return query_one("SELECT * FROM app_environments WHERE app_id=? AND id=?",
                         (app_id, env_id))
    return query_one("SELECT * FROM app_environments WHERE app_id=? AND env_key=?",
                     (app_id, env_key))


def _usage_counts(app_id: int, env_key: str, bl_id: int) -> dict:
    items = query_one(
        "SELECT COUNT(*) AS c FROM config_items WHERE app_id=? AND environment=?",
        (app_id, env_key),
    )["c"]
    versions = query_one(
        "SELECT COUNT(*) AS c FROM config_versions WHERE app_id=? AND environment=?",
        (app_id, env_key),
    )["c"]
    instances = query_one(
        "SELECT COUNT(*) AS c FROM app_instances WHERE app_id=? AND environment=?",
        (app_id, env_key),
    )["c"]
    grants = query_one(
        "SELECT COUNT(*) AS c FROM user_grants WHERE business_line_id=? AND environment=?",
        (bl_id, env_key),
    )["c"]
    return {"config_items": items, "config_versions": versions,
            "instances": instances, "grants": grants}


def list_environments(app_id: int, app_row: dict, at: int | None = None) -> list[dict]:
    rows = query(
        "SELECT * FROM app_environments WHERE app_id=? ORDER BY "
        "CASE env_key WHEN 'dev' THEN 0 WHEN 'test' THEN 1 WHEN 'staging' THEN 2 "
        "WHEN 'prod' THEN 3 ELSE 4 END, id",
        (app_id,),
    )
    result = []
    for r in rows:
        d = env_to_dict(r, app_row, at=at)
        result.append(d)
    return result


def env_to_dict(row, app_row: dict | None = None, at: int | None = None) -> dict:
    now = at if at is not None else int(time.time())
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    holidays = query(
        "SELECT id, holiday_date, reason FROM env_holidays WHERE env_id=? ORDER BY holiday_date",
        (row["id"],),
    )
    upcoming = [
        {"id": h["id"], "date": h["holiday_date"], "reason": h["reason"],
         "passed": h["holiday_date"] < today}
        for h in holidays
    ]
    window = evaluate_window(row, at=at)
    counts = _usage_counts(row["app_id"], row["env_key"],
                           app_row["business_line_id"] if app_row else 0)
    health = env_health(row["app_id"], row["env_key"], at=at)
    is_primary = bool(app_row and app_row["environment"] == row["env_key"])
    blockers = []
    if is_primary:
        blockers.append("该环境是应用当前登记的所属环境，请先在应用信息里调整所属环境")
    if counts["config_items"]:
        blockers.append(f"挂着 {counts['config_items']} 个配置项")
    if counts["config_versions"]:
        blockers.append(f"挂着 {counts['config_versions']} 个配置历史版本")
    if counts["instances"]:
        blockers.append(f"挂着 {counts['instances']} 个运行实例")
    return {
        "id": row["id"],
        "app_id": row["app_id"],
        "env_key": row["env_key"],
        "env_label": row["env_label"],
        "is_builtin": bool(row["is_builtin"]),
        "from_upstream": row["source_id"] is not None,
        "source_deleted": bool(row["source_deleted"]),
        "is_primary": is_primary,
        "deploy_restricted": bool(row["deploy_restricted"]),
        "window_days": json.loads(row["window_days"] or "[]"),
        "window_start": row["window_start"],
        "window_end": row["window_end"],
        "window_text": window["window_text"],
        "window_open_now": window["allowed"],
        "window_status": window,
        "holidays": upcoming,
        "usage": counts,
        "health": health,
        "can_delete": not blockers,
        "delete_blockers": blockers,
        "updated_at": row["updated_at"],
    }


def create_environment(app_id: int, app_row: dict, env_key: str, env_label: str,
                       user: dict, at: int | None = None) -> dict:
    key = (env_key or "").strip().lower()
    label = (env_label or "").strip()
    if not env_key_ok(key):
        raise ValueError("环境标识非法：需小写字母开头，仅含小写字母、数字、_、-，长度 ≤32（如 gray、pre-prod）")
    if not label:
        raise ValueError("环境名称不能为空（如：灰度、预演）")
    if get_env_row(app_id, env_key=key):
        raise ValueError(f"环境标识「{key}」在该应用下已存在")
    if query_one("SELECT 1 FROM app_environments WHERE app_id=? AND env_label=?",
                 (app_id, label)):
        raise ValueError(f"环境名称「{label}」在该应用下已存在")
    now = at or int(time.time())
    cur = get_conn().execute(
        """INSERT INTO app_environments
           (app_id, env_key, env_label, is_builtin, deploy_restricted,
            window_days, window_start, window_end, created_by, created_at, updated_at)
           VALUES (?,?,?,0,0,'[]','00:00','23:59',?,?,?)""",
        (app_id, key, label, user["id"], now, now),
    )
    get_conn().commit()
    log_ops(app_id, key, label, "env_create", label,
            f"新增自定义环境「{label}」（标识 {key}），默认全时段允许发布", user["id"], now)
    row = get_env_row(app_id, env_id=cur.lastrowid)
    return env_to_dict(row, app_row)


def update_window(app_id: int, env_id: int, app_row: dict, user: dict,
                  deploy_restricted: bool, window_days: list,
                  window_start: str, window_end: str) -> dict:
    row = get_env_row(app_id, env_id=env_id)
    if not row:
        raise LookupError("环境不存在")
    days = sorted({int(d) for d in window_days if str(d).isdigit() and 0 <= int(d) <= 6})
    start = (window_start or "").strip()
    end = (window_end or "").strip()
    if deploy_restricted:
        if not days:
            raise ValueError("开启发布窗口限制后，至少选择一周中的一天")
        if not hhmm_ok(start) or not hhmm_ok(end):
            raise ValueError("时间格式应为 HH:MM（如 10:00）")
        if start >= end:
            raise ValueError("窗口开始时间必须早于结束时间（暂不支持跨午夜窗口）")
    old = env_to_dict(row, app_row)
    now = int(time.time())
    get_conn().execute(
        """UPDATE app_environments
           SET deploy_restricted=?, window_days=?, window_start=?, window_end=?, updated_at=?
           WHERE id=?""",
        (1 if deploy_restricted else 0, json.dumps(days),
         start if deploy_restricted else "00:00",
         end if deploy_restricted else "23:59", now, env_id),
    )
    get_conn().commit()
    if deploy_restricted:
        new_desc = f"每周 {_weekdays_text(days)} {start}–{end}"
    else:
        new_desc = "全时段允许发布"
    detail = f"发布窗口调整：{old['window_text']} → {new_desc}"
    log_ops(app_id, row["env_key"], row["env_label"], "window_update",
            row["env_label"], detail, user["id"], now)
    return env_to_dict(get_env_row(app_id, env_id=env_id), app_row)


def delete_environment(app_id: int, env_id: int, app_row: dict, user: dict) -> None:
    row = get_env_row(app_id, env_id=env_id)
    if not row:
        raise LookupError("环境不存在")
    info = env_to_dict(row, app_row)
    if info["delete_blockers"]:
        raise EnvDeleteBlockedError({
            "code": "env_delete_blocked",
            "message": (f"环境「{row['env_label']}」不能删除："
                        + "；".join(info["delete_blockers"]) + "。"),
            "environment": row["env_key"],
            "environment_label": row["env_label"],
            "blockers": info["delete_blockers"],
            "usage": info["usage"],
        })
    now = int(time.time())
    get_conn().execute("DELETE FROM app_environments WHERE id=?", (env_id,))
    get_conn().commit()
    # 同步墓碑：本地删除的上游环境，上游再推来时不自动复活（局部导入避免循环依赖）
    try:
        from . import sync_service as sync
        app = query_one("SELECT source_id FROM applications WHERE id=?", (app_id,))
        sync._write_tombstone("env", row["source_id"], app["source_id"] if app else "",
                              row["env_label"], user["id"], now)
        sync._close_pending_on_delete("env", row["source_id"], user, now)
    except Exception:  # 同步子系统异常不应阻断环境删除主流程
        pass
    log_ops(app_id, row["env_key"], row["env_label"], "env_delete", row["env_label"],
            f"删除环境「{row['env_label']}」（标识 {row['env_key']}，删除前无配置与实例挂载）",
            user["id"], now)


# ---------------------------------------------------------------- 节假日封网

def add_holiday(app_id: int, env_id: int, app_row: dict, user: dict,
                date: str, reason: str) -> dict:
    row = get_env_row(app_id, env_id=env_id)
    if not row:
        raise LookupError("环境不存在")
    date = (date or "").strip()
    if not DATE_RE.fullmatch(date):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("日期不合法")
    if query_one("SELECT 1 FROM env_holidays WHERE env_id=? AND holiday_date=?",
                 (env_id, date)):
        raise ValueError(f"{date} 已在封网日期中")
    note = (reason or "").strip()
    now = int(time.time())
    cur = get_conn().execute(
        """INSERT INTO env_holidays (env_id, app_id, holiday_date, reason, created_by, created_at)
           VALUES (?,?,?,?,?,?)""",
        (env_id, app_id, date, note, user["id"], now),
    )
    get_conn().commit()
    log_ops(app_id, row["env_key"], row["env_label"], "holiday_add", date,
            f"新增封网日 {date}" + (f"（{note}）" if note else "，当天关闭发布"),
            user["id"], now)
    return {"id": cur.lastrowid, "date": date, "reason": note}


def remove_holiday(app_id: int, env_id: int, holiday_id: int, app_row: dict,
                   user: dict) -> None:
    row = get_env_row(app_id, env_id=env_id)
    if not row:
        raise LookupError("环境不存在")
    h = query_one("SELECT * FROM env_holidays WHERE id=? AND env_id=?", (holiday_id, env_id))
    if not h:
        raise LookupError("封网日期不存在")
    get_conn().execute("DELETE FROM env_holidays WHERE id=?", (holiday_id,))
    get_conn().commit()
    log_ops(app_id, row["env_key"], row["env_label"], "holiday_remove", h["holiday_date"],
            f"解除封网日 {h['holiday_date']}" + (f"（{h['reason']}）" if h["reason"] else ""),
            user["id"])


# ---------------------------------------------------------------- 实例健康

def _restart_counts(instance_id: int, at: int) -> tuple[int, int]:
    """返回 (近 24h 重启次数, 近 7 天重启次数)。"""
    r24 = query_one(
        "SELECT COUNT(*) AS c FROM instance_events WHERE instance_id=? AND event_type='restart' AND created_at>=?",
        (instance_id, at - 86400),
    )["c"]
    r7 = query_one(
        "SELECT COUNT(*) AS c FROM instance_events WHERE instance_id=? AND event_type='restart' AND created_at>=?",
        (instance_id, at - 7 * 86400),
    )["c"]
    return r24, r7


def instance_level(status: str, r24: int, r7: int) -> str:
    if status == "offline":
        return "offline"
    if r24 >= CRASH_24H_CRITICAL:
        return "critical"
    if r24 >= CRASH_24H_WARNING or r7 >= CRASH_7D_WARNING:
        return "warning"
    return "normal"


def _instance_to_dict(r, at: int | None = None) -> dict:
    now = at if at is not None else int(time.time())
    r24, r7 = _restart_counts(r["id"], now)
    level = instance_level(r["status"], r24, r7)
    offline_since = None
    if r["status"] == "offline":
        ev = query_one(
            "SELECT created_at FROM instance_events WHERE instance_id=? AND event_type='offline' "
            "ORDER BY created_at DESC LIMIT 1",
            (r["id"],),
        )
        offline_since = ev["created_at"] if ev else r["updated_at"]
    return {
        "id": r["id"],
        "app_id": r["app_id"],
        "environment": r["environment"],
        "name": r["name"],
        "status": r["status"],
        "status_label": "存活" if r["status"] == "alive" else "掉线",
        "restart_count": r["restart_count"],
        "restarts_24h": r24,
        "restarts_7d": r7,
        "last_restart_at": r["last_restart_at"],
        "last_seen_at": r["last_seen_at"],
        "offline_since": offline_since,
        "level": level,
        "level_label": LEVEL_LABELS[level],
        "created_at": r["created_at"],
    }


def list_instances(app_id: int, env_key: str, at: int | None = None) -> list[dict]:
    rows = query(
        "SELECT * FROM app_instances WHERE app_id=? AND environment=? ORDER BY "
        "CASE status WHEN 'offline' THEN 0 ELSE 1 END, name",
        (app_id, env_key),
    )
    now = at if at is not None else int(time.time())
    return [_instance_to_dict(r, now) for r in rows]


def list_instance_events(instance_id: int, limit: int = 20) -> list[dict]:
    rows = query(
        """SELECT e.*, u.name AS actor_name FROM instance_events e
           LEFT JOIN users u ON u.id=e.actor_id
           WHERE e.instance_id=? ORDER BY e.created_at DESC, e.id DESC LIMIT ?""",
        (instance_id, limit),
    )
    labels = {"restart": "重启", "offline": "掉线", "recover": "恢复"}
    return [{
        "id": r["id"], "event_type": r["event_type"],
        "event_label": labels.get(r["event_type"], r["event_type"]),
        "detail": r["detail"], "actor_name": r["actor_name"] or "监控系统",
        "created_at": r["created_at"],
    } for r in rows]


def env_health(app_id: int, env_key: str, at: int | None = None) -> dict:
    now = at if at is not None else int(time.time())
    instances = list_instances(app_id, env_key, now)
    total = len(instances)
    offline = [i for i in instances if i["status"] == "offline"]
    critical = [i for i in instances if i["level"] == "critical"]
    warning = [i for i in instances if i["level"] == "warning"]
    r24 = sum(i["restarts_24h"] for i in instances)
    last_restart = max((i["last_restart_at"] or 0) for i in instances) if instances else None
    if offline:
        rollup = "offline"
    elif critical:
        rollup = "critical"
    elif warning:
        rollup = "warning"
    elif total:
        rollup = "normal"
    else:
        rollup = "empty"
    return {
        "total": total,
        "alive": total - len(offline),
        "offline": len(offline),
        "critical_count": len(critical),
        "warning_count": len(warning),
        "restarts_24h": r24,
        "last_restart_at": last_restart,
        "level": rollup,
        "level_label": {"offline": "有实例掉线", "critical": "频繁重启",
                        "warning": "重启偏多", "normal": "健康",
                        "empty": "未登记实例"}[rollup],
        "instances": instances,
    }


def all_env_health(user: dict, at: int | None = None) -> list[dict]:
    """跨应用的环境健康卡片（按可见范围收窄），作战台与健康页用。"""
    from . import permissions as perms
    sql = """SELECT e.id AS env_id, e.env_key, e.env_label, e.app_id, a.name AS app_name,
                    a.status AS app_status, a.owner_id, a.business_line_id, b.name AS business_line_name
             FROM app_environments e
             JOIN applications a ON a.id=e.app_id
             JOIN business_lines b ON b.id=a.business_line_id
             WHERE 1=1"""
    params: list = []
    if not perms.is_admin(user):
        cond, cp = perms.scope_condition(user, "a.business_line_id", "e.env_key", "a.owner_id")
        sql += f" AND {cond}"
        params.extend(cp)
    sql += " ORDER BY a.business_line_id, a.id, e.id"
    now = at if at is not None else int(time.time())
    result = []
    for r in query(sql, tuple(params)):
        h = env_health(r["app_id"], r["env_key"], now)
        if h["total"] == 0:
            continue
        result.append({
            "app_id": r["app_id"], "app_name": r["app_name"], "app_status": r["app_status"],
            "business_line_id": r["business_line_id"], "business_line_name": r["business_line_name"],
            "env_id": r["env_id"], "environment": r["env_key"], "environment_label": r["env_label"],
            "health": h,
        })
    return result


def console_alerts(user: dict, at: int | None = None) -> dict:
    """作战台健康告警：掉线实例必须直接冒出来；频繁重启环境单独分组。"""
    cards = all_env_health(user, at)
    offline_items, restart_envs = [], []
    for c in cards:
        h = c["health"]
        for i in h["instances"]:
            if i["status"] == "offline":
                offline_items.append({
                    "app_id": c["app_id"], "app_name": c["app_name"],
                    "business_line_id": c["business_line_id"],
                    "business_line_name": c["business_line_name"],
                    "environment": c["environment"], "environment_label": c["environment_label"],
                    "instance_id": i["id"], "instance_name": i["name"],
                    "offline_since": i["offline_since"], "last_seen_at": i["last_seen_at"],
                    "restart_count": i["restart_count"],
                })
        # 环境级重启告警：24h 频繁（红）或 7 天偏多（黄）都要冒出来，
        # 但只显示重启次数，避免与半个月一次的正常重启混为一谈
        if h["critical_count"]:
            level = "critical"
        elif h["warning_count"]:
            level = "warning"
        else:
            level = None
        if level:
            restart_envs.append({
                "app_id": c["app_id"], "app_name": c["app_name"],
                "business_line_id": c["business_line_id"],
                "business_line_name": c["business_line_name"],
                "environment": c["environment"], "environment_label": c["environment_label"],
                "restarts_24h": h["restarts_24h"], "critical_count": h["critical_count"],
                "warning_count": h["warning_count"], "level": level,
                "level_label": LEVEL_LABELS[level],
                "last_restart_at": h["last_restart_at"],
            })
    offline_items.sort(key=lambda x: x["offline_since"] or 0)
    restart_envs.sort(key=lambda x: (0 if x["level"] == "critical" else 1, -x["restarts_24h"]))
    return {
        "offline_instances": offline_items,
        "restart_envs": restart_envs,
        "totals": {
            "offline_instances": len(offline_items),
            "restart_envs": len(restart_envs),
            "restart_env_critical": sum(1 for x in restart_envs if x["level"] == "critical"),
        },
    }


# ---------------------------------------------------------------- 实例动作（演示环境用，模拟监控上报）

def _get_instance(instance_id: int):
    return query_one("SELECT * FROM app_instances WHERE id=?", (instance_id,))


def register_instance(app_id: int, env_key: str, name: str, user: dict | None) -> dict:
    env = get_env_row(app_id, env_key=env_key)
    if not env:
        raise LookupError(f"该应用下不存在环境「{env_key}」，请先新增环境")
    name = (name or "").strip()
    if not name:
        raise ValueError("实例名不能为空")
    if query_one("SELECT 1 FROM app_instances WHERE app_id=? AND env_id=? AND name=?",
                 (app_id, env["id"], name)):
        raise ValueError(f"实例名「{name}」在该环境已存在")
    now = int(time.time())
    cur = get_conn().execute(
        """INSERT INTO app_instances
           (app_id, env_id, environment, name, status, restart_count,
            last_restart_at, last_seen_at, created_at, updated_at)
           VALUES (?,?,?,?,'alive',0,NULL,?,?,?)""",
        (app_id, env["id"], env_key, name, now, now, now),
    )
    get_conn().commit()
    log_ops(app_id, env_key, env["env_label"], "instance_register", name,
            f"登记实例 {name}", user["id"] if user else None, now)
    return _instance_to_dict(_get_instance(cur.lastrowid), now)


def remove_instance(instance_id: int, user: dict | None) -> None:
    inst = _get_instance(instance_id)
    if not inst:
        raise LookupError("实例不存在")
    env = get_env_row(inst["app_id"], env_id=inst["env_id"])
    now = int(time.time())
    get_conn().execute("DELETE FROM app_instances WHERE id=?", (instance_id,))
    get_conn().commit()
    log_ops(inst["app_id"], inst["environment"], env["env_label"] if env else inst["environment"],
            "instance_remove", inst["name"], f"移除实例 {inst['name']}",
            user["id"] if user else None, now)


def restart_instance(instance_id: int, user: dict | None, detail: str = "") -> dict:
    inst = _get_instance(instance_id)
    if not inst:
        raise LookupError("实例不存在")
    now = int(time.time())
    get_conn().execute(
        """UPDATE app_instances SET restart_count=restart_count+1,
           last_restart_at=?, last_seen_at=?, status='alive', updated_at=? WHERE id=?""",
        (now, now, now, instance_id),
    )
    get_conn().execute(
        """INSERT INTO instance_events
           (instance_id, app_id, env_id, environment, event_type, detail, actor_id, created_at)
           VALUES (?,?,?,?,'restart',?,?,?)""",
        (instance_id, inst["app_id"], inst["env_id"], inst["environment"],
         (detail or "手动触发重启/监控记录重启"), user["id"] if user else None, now),
    )
    get_conn().commit()
    env = get_env_row(inst["app_id"], env_id=inst["env_id"])
    n = inst["restart_count"] + 1
    log_ops(inst["app_id"], inst["environment"], env["env_label"] if env else inst["environment"],
            "instance_restart", inst["name"], f"实例 {inst['name']} 第 {n} 次重启",
            user["id"] if user else None, now)
    return _instance_to_dict(_get_instance(instance_id), now)


def mark_offline(instance_id: int, user: dict | None, detail: str = "") -> dict:
    inst = _get_instance(instance_id)
    if not inst:
        raise LookupError("实例不存在")
    if inst["status"] == "offline":
        raise ValueError("该实例已是掉线状态")
    now = int(time.time())
    get_conn().execute(
        "UPDATE app_instances SET status='offline', updated_at=? WHERE id=?", (now, instance_id))
    get_conn().execute(
        """INSERT INTO instance_events
           (instance_id, app_id, env_id, environment, event_type, detail, actor_id, created_at)
           VALUES (?,?,?,?,'offline',?,?,?)""",
        (instance_id, inst["app_id"], inst["env_id"], inst["environment"],
         (detail or "心跳超时，监控判定掉线"), user["id"] if user else None, now),
    )
    get_conn().commit()
    env = get_env_row(inst["app_id"], env_id=inst["env_id"])
    log_ops(inst["app_id"], inst["environment"], env["env_label"] if env else inst["environment"],
            "instance_offline", inst["name"],
            f"实例 {inst['name']} 掉线（最后存活 {time.strftime('%H:%M', time.localtime(inst['last_seen_at']))}）",
            user["id"] if user else None, now)
    return _instance_to_dict(_get_instance(instance_id), now)


def mark_recover(instance_id: int, user: dict | None, detail: str = "") -> dict:
    inst = _get_instance(instance_id)
    if not inst:
        raise LookupError("实例不存在")
    if inst["status"] != "offline":
        raise ValueError("该实例当前存活，无需恢复")
    now = int(time.time())
    get_conn().execute(
        "UPDATE app_instances SET status='alive', last_seen_at=?, updated_at=? WHERE id=?",
        (now, now, instance_id))
    get_conn().execute(
        """INSERT INTO instance_events
           (instance_id, app_id, env_id, environment, event_type, detail, actor_id, created_at)
           VALUES (?,?,?,?,'recover',?,?,?)""",
        (instance_id, inst["app_id"], inst["env_id"], inst["environment"],
         (detail or "心跳恢复"), user["id"] if user else None, now),
    )
    get_conn().commit()
    env = get_env_row(inst["app_id"], env_id=inst["env_id"])
    log_ops(inst["app_id"], inst["environment"], env["env_label"] if env else inst["environment"],
            "instance_recover", inst["name"], f"实例 {inst['name']} 恢复存活",
            user["id"] if user else None, now)
    return _instance_to_dict(_get_instance(instance_id), now)


# ---------------------------------------------------------------- 留痕

def log_ops(app_id: int, env_key: str, env_label: str, category: str,
            target_name: str, detail: str, actor_id: int | None,
            created_at: int | None = None) -> None:
    get_conn().execute(
        """INSERT INTO ops_audit_logs
           (app_id, environment, environment_label, category, target_name, detail, actor_id, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (app_id, env_key, env_label, category, target_name, detail, actor_id,
         created_at or int(time.time())),
    )
    get_conn().commit()


def _date_to_epoch(value: str | None, end: bool = False) -> int | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        t = time.strptime(value + (" 23:59:59" if end else " 00:00:00"), "%Y-%m-%d %H:%M:%S")
        return int(time.mktime(t))
    if re.fullmatch(r"\d{10}", value):
        return int(value)
    raise ValueError("时间格式应为 YYYY-MM-DD")


def query_ops_audit(user: dict, *, app_id=None, business_line_id=None, environment=None,
                    category=None, start=None, end=None) -> tuple[str, list]:
    """环境与健康留痕查询（SQL, params），按可见范围强制收窄。"""
    from . import permissions as perms
    if category and category not in CATEGORY_LABELS:
        raise ValueError(f"非法类别：{category}")
    sql = """SELECT l.id, l.app_id, l.environment, l.environment_label, l.category,
                    l.target_name, l.detail, l.created_at,
                    a.name AS app_name, a.owner_id,
                    b.id AS business_line_id, b.name AS business_line_name,
                    u.name AS user_name
             FROM ops_audit_logs l
             JOIN applications a ON a.id=l.app_id
             JOIN business_lines b ON b.id=a.business_line_id
             LEFT JOIN users u ON u.id=l.actor_id
             WHERE 1=1"""
    params: list = []
    if perms.is_admin(user):
        if business_line_id:
            sql += " AND a.business_line_id=?"
            params.append(business_line_id)
    else:
        cond, cp = perms.scope_condition(user, "a.business_line_id", "l.environment", "a.owner_id")
        sql += f" AND {cond}"
        params.extend(cp)
        if business_line_id:
            sql += " AND a.business_line_id=?"
            params.append(business_line_id)
    if app_id:
        sql += " AND l.app_id=?"
        params.append(app_id)
    if environment:
        sql += " AND l.environment=?"
        params.append(environment)
    if category:
        sql += " AND l.category=?"
        params.append(category)
    start_ts = _date_to_epoch(start)
    end_ts = _date_to_epoch(end, end=True)
    if start_ts is not None:
        sql += " AND l.created_at>=?"
        params.append(start_ts)
    if end_ts is not None:
        sql += " AND l.created_at<=?"
        params.append(end_ts)
    sql += " ORDER BY l.created_at DESC, l.id DESC LIMIT 500"
    return sql, params


def ops_row_to_dict(r) -> dict:
    env_label = r["environment_label"] or ENV_LABELS.get(r["environment"], r["environment"])
    return {
        "id": r["id"],
        "app_id": r["app_id"],
        "app_name": r["app_name"],
        "business_line_name": r["business_line_name"],
        "environment": r["environment"],
        "environment_label": env_label,
        "category": r["category"],
        "category_label": CATEGORY_LABELS.get(r["category"], r["category"]),
        "target_name": r["target_name"],
        "detail": r["detail"],
        "user_name": r["user_name"] or "监控系统",
        "created_at": r["created_at"],
    }


OPS_CSV_HEADER = ["时间", "业务线", "应用", "环境", "类别", "对象", "详情", "操作人"]


def export_ops_csv(rows) -> str:
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(OPS_CSV_HEADER)
    for r in rows:
        d = ops_row_to_dict(r)
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"])),
            d["business_line_name"], d["app_name"], d["environment_label"],
            d["category_label"], d["target_name"], d["detail"], d["user_name"],
        ])
    return buf.getvalue()
