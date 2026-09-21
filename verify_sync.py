"""同步功能端到端验证（不依赖 FastAPI，直跑 db/seed/sync_source/sync_service）。"""
import os
import sys
import tempfile

tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(tmp, "test.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import init_db, query, query_one, get_conn  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app import env_service as esvc  # noqa: E402
from app import sync_service as sync  # noqa: E402
from app import sync_source as source  # noqa: E402

ADMIN = {"id": 1, "name": "系统管理员", "role": "admin"}
failures = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


init_db()
seed_if_empty()
print("== 种子完成（已自动对齐模拟源）==")

# ---------- 第 0 趟：全部无变化 ----------
r = sync.run_sync("manual", ADMIN)
t = r["totals"]
print(f"== 第 0 趟: {t} ==")
check("首轮无新增", t["created"] == 0, str(t))
check("首轮无改动", t["updated"] == 0, str(t))
check("首轮无冲突", t["conflicts"] == 0 and t["upstream_deleted"] == 0 and t["local_deleted"] == 0, str(t))
check("首轮无未通过", t["invalid"] == 0, str(t))
check("首轮全部无变化", t["unchanged"] > 30, str(t))

# ---------- 第一幕 ----------
adv = source.advance_scenario(ADMIN)
print(f"== 演进第 1 幕，{len(adv['changes'])} 个上游变更 ==")
r = sync.run_sync("manual", ADMIN)
t = r["totals"]
print(f"== 第 1 趟: {t} ==")
check("新增应用1个(智能客服平台)", t["created"] >= 2, str(t))  # app + env + module
check("有自动改动", t["updated"] >= 2, str(t))
check("上游删除待决1个(对账平台)", t["upstream_deleted"] == 1, str(t))
check("非法数据1个(火星9集群)", t["invalid"] == 1, str(t))

cs = query_one("SELECT * FROM applications WHERE name='智能客服平台'")
check("新应用已落库", cs is not None)
check("新应用负责人空缺", cs and cs["owner_id"] is None)
check("新应用带基线", cs and cs["sync_baseline_json"] != "{}")
bad = query_one("SELECT id FROM applications WHERE name='灰度影子应用'")
check("非法应用未落库", bad is None)
dz = query_one("SELECT * FROM applications WHERE name='对账平台'")
check("对账平台仍在列表且标记上游删除", dz and dz["source_deleted"] == 1)
conf_dz = query_one("SELECT id FROM sync_conflicts WHERE source_id=? AND kind='upstream_deleted' AND status='pending'", (dz["source_id"],))
check("对账平台有删除待决冲突", conf_dz is not None)
# 未通过原因落明细
inv = query("SELECT reason FROM sync_items WHERE run_id=? AND result='invalid'", (r["run_id"],))
check("未通过明细写明原因", any("火星9集群" in x["reason"] for x in inv), str([x["reason"] for x in inv]))

# 新应用的环境与模块
cs_env = query_one("SELECT * FROM app_environments WHERE app_id=? AND env_key='dev'", (cs["id"],))
check("新应用开发环境已建", cs_env is not None)
cs_mod = query_one("SELECT * FROM app_modules WHERE app_id=? AND name='会话核心服务'", (cs["id"],))
check("新应用模块已建", cs_mod is not None)

# 支付网关描述被上游自动更新
gw = query_one("SELECT * FROM applications WHERE name='支付网关'")
check("支付网关自动采用上游值", "合单支付" in gw["description"], gw["description"])

# ---------- 第二幕：双方同改 + 本地已删 ----------
source.advance_scenario(ADMIN)
print("== 演进第 2 幕（双方同改/本地已删）==")
r2 = sync.run_sync("manual", ADMIN)
t2 = r2["totals"]
print(f"== 第 2 趟: {t2} ==")
check("产生双方同改冲突", t2["conflicts"] == 2, str(t2))  # 支付网关 + 会员模块
check("本地已删待决1个(积分商城)", t2["local_deleted"] == 1, str(t2))

# 支付网关本地值未被覆盖
gw2 = query_one("SELECT * FROM applications WHERE name='支付网关'")
check("本地值未被上游悄悄覆盖", "华东双活" in gw2["description"], gw2["description"])
check("基线未被推进", "合单支付" in sync._loads(gw2["sync_baseline_json"]).get("description", ""))
gw_conf = query_one("SELECT * FROM sync_conflicts WHERE source_id=? AND kind='both_changed' AND status='pending'", (gw2["source_id"],))
check("支付网关冲突挂起", gw_conf is not None)

# 裁决：支付网关保留本地
sync.decide_conflict(gw_conf["id"], "keep_local", "本地为线上热修，暂不采用", ADMIN)
ack = sync._loads(query_one("SELECT sync_upstream_ack_json FROM applications WHERE id=?", (gw2["id"],))["sync_upstream_ack_json"])
check("裁决后记录驳回的上游值", "reject" in ack and "跨境钱包" in ack["reject"].get("description", ""))
r3 = sync.run_sync("scheduled", None)
t3 = r3["totals"]
check("同一上游值不再重复报支付网关冲突",
      t3["conflicts"] == 1, str(t3))  # 仅剩会员模块冲突，支付网关已驳回不再报
gw_items = query("SELECT result FROM sync_items WHERE run_id=? AND source_id=? AND entity='app'",
                 (r3["run_id"], gw2["source_id"]))
check("支付网关本趟无变化", all(x["result"] == "unchanged" for x in gw_items), str([x["result"] for x in gw_items]))
# 裁决留痕字段
gw_conf2 = query_one("SELECT * FROM sync_conflicts WHERE id=?", (gw_conf["id"],))
check("裁决记录决定人/备注/时间", gw_conf2["decided_by"] == 1 and bool(gw_conf2["decided_at"]) and "热修" in gw_conf2["decision_note"])

# 会员模块冲突：采用上游
mod_conf = query_one("""SELECT c.* FROM sync_conflicts c
    JOIN app_modules m ON m.id=c.local_id
    WHERE c.kind='both_changed' AND c.status='pending'
      AND m.name='核心服务' AND c.app_id=(SELECT id FROM applications WHERE name='会员中心')""")
check("会员模块冲突存在", mod_conf is not None)
sync.decide_conflict(mod_conf["id"], "take_upstream", "确认发版", ADMIN)
mm = query_one("SELECT * FROM app_modules WHERE id=?", (mod_conf["local_id"],))
check("采用上游后版本=v2.4.2", mm["version_tag"] == "v2.4.2", mm["version_tag"])
check("采用上游后基线已推进", sync._loads(mm["sync_baseline_json"]).get("version_tag") == "v2.4.2")

# 积分商城本地已删：墓碑存在、应用确实不在
jf_src = next(x for x in source.fetch_source() if x["entity"] == "app" and x["name"] == "积分商城")
jf_tomb = query_one("SELECT * FROM sync_tombstones WHERE entity='app' AND source_id=?", (jf_src["source_id"],))
check("积分商城有删除墓碑", jf_tomb is not None)
check("积分商城未复活", query_one("SELECT id FROM applications WHERE name='积分商城'") is None)
jf_conf = query_one("SELECT id FROM sync_conflicts WHERE source_id=? AND kind='local_deleted' AND status='pending'", (jf_src["source_id"],))
check("积分商城挂本地已删待决", jf_conf is not None)
# 再跑一趟：保持待决但不复活
r4 = sync.run_sync("manual", ADMIN)
t4 = r4["totals"]
check("再跑一趟仍不复活", query_one("SELECT id FROM applications WHERE name='积分商城'") is None)
check("本地已删持续计入", t4["local_deleted"] >= 1, str(t4))
# 裁决：恢复
sync.decide_conflict(jf_conf["id"], "resurrect", "业务恢复", ADMIN)
check("裁决恢复后应用复活", query_one("SELECT id FROM applications WHERE name='积分商城'") is not None)
check("恢复后墓碑清除", query_one("SELECT 1 FROM sync_tombstones WHERE source_id=?", (jf_src["source_id"],)) is None)

# ---------- 第三幕：环境删除 / 状态回退 / 模块删除 ----------
source.advance_scenario(ADMIN)
print("== 演进第 3 幕（环境删除/非法回退/模块删除）==")
r5 = sync.run_sync("manual", ADMIN)
t5 = r5["totals"]
print(f"== 第 5 趟: {t5} ==")
check("上游删除待决含灰度环境", t5["upstream_deleted"] >= 2, str(t5))
check("状态回退被判未通过", t5["invalid"] >= 1, str(t5))
rollback_item = query("SELECT reason FROM sync_items WHERE run_id=? AND result='invalid'", (r5["run_id"],))
check("未通过原因含状态回退", any("回退" in x["reason"] for x in rollback_item), str([x["reason"] for x in rollback_item]))
rk = query_one("SELECT status FROM applications WHERE name='风控实时引擎'")
check("风控状态仍为上线(未回退)", rk["status"] == "online", rk["status"])
gray = query_one("""SELECT e.* FROM app_environments e JOIN applications a ON a.id=e.app_id
                    WHERE a.name='会员中心' AND e.env_key='gray'""")
check("灰度环境仍保留并标记", gray and gray["source_deleted"] == 1)

# 跟随上游删除灰度环境：该环境种子里有 1 个实例，挂载清点应先拦截；移除实例后可删
gray_conf = query_one("SELECT id FROM sync_conflicts WHERE entity='env' AND source_id=? AND status='pending'", (gray["source_id"],))
blocked = False
try:
    sync.decide_conflict(gray_conf["id"], "delete_local", "跟随清理", ADMIN)
except esvc.EnvDeleteBlockedError:
    blocked = True
check("有实例挂载时跟随删除被拦截并说明", blocked)
check("灰度环境仍在（未被强删）", query_one("SELECT id FROM app_environments WHERE id=?", (gray["id"],)) is not None)
get_conn().execute("DELETE FROM app_instances WHERE env_id=?", (gray["id"],))
get_conn().commit()
sync.decide_conflict(gray_conf["id"], "delete_local", "实例已迁移，跟随清理", ADMIN)
check("灰度环境已跟随删除", query_one("SELECT id FROM app_environments WHERE id=?", (gray["id"],)) is None)
check("灰度环境有墓碑", query_one("SELECT 1 FROM sync_tombstones WHERE entity='env' AND source_id=?", (gray["source_id"],)) is not None)

# 消息推送模块上游删除 → 保留本地
mp_conf = query_one("""SELECT c.id FROM sync_conflicts c JOIN app_modules m ON m.id=c.local_id
    JOIN applications a ON a.id=m.app_id
    WHERE c.kind='upstream_deleted' AND a.name='消息推送中心' AND m.name='核心服务' AND c.status='pending'""")
check("消息模块删除待决存在", mp_conf is not None)
sync.decide_conflict(mp_conf["id"], "keep_local", "模块仍在用", ADMIN)
mpm = query_one("""SELECT m.* FROM app_modules m JOIN applications a ON a.id=m.app_id
                   WHERE a.name='消息推送中心' AND m.name='核心服务'""")
check("保留本地后模块仍在且标记清除", mpm and mpm["source_deleted"] == 0)

# 对账平台：跟随上游删除（应用级，confirm 级联）
dz_conf = query_one("SELECT id FROM sync_conflicts WHERE entity='app' AND source_id=? AND kind='upstream_deleted'", (dz["source_id"],))
mounts = sync.conflict_to_dict(query_one("SELECT * FROM sync_conflicts WHERE id=?", (dz_conf["id"],)))["delete_mounts"]
check("应用删除裁决带挂载清点", mounts is not None and mounts["environments"] >= 4, str(mounts))
sync.decide_conflict(dz_conf["id"], "delete_local", "跟随上游退役", ADMIN)
check("对账平台已级联删除", query_one("SELECT id FROM applications WHERE name='对账平台'") is None)
check("对账应用有墓碑", query_one("SELECT 1 FROM sync_tombstones WHERE source_id=?", (dz["source_id"],)) is not None)
r6 = sync.run_sync("manual", ADMIN)
t6 = r6["totals"]
dz_items = query("SELECT result, reason FROM sync_items WHERE run_id=? AND source_id=?", (r6["run_id"], dz["source_id"]))
check("已裁决跟随删除的不再出现", all(x["result"] == "ignored" for x in dz_items), str([dict(x) for x in dz_items]))

# ---------- 运行留痕 ----------
runs = sync.list_runs(50)
check("趟次记录完整", len(runs) >= 6, str(len(runs)))
last = sync.get_run(r["run_id"])
check("趟次有触发人", last["triggered_by_name"] == "系统管理员")
check("趟次有耗时", last["duration_ms"] is not None and last["duration_ms"] >= 0)
check("趟次有起止时间", last["finished_at"] is not None)
sched = [x for x in runs if x["trigger_type"] == "scheduled"]
check("定时趟次触发人显示系统调度", sched and sched[0]["triggered_by_name"] == "系统定时调度")
check("每趟逐对象明细存在", len(last["items"]) == last["totals"]["received"])

# 待裁决计数
pc = sync.pending_counts()
print(f"== 剩余待裁决: {pc} ==")

# ---------- 调度设置 ----------
s = sync.update_settings(True, 120, ADMIN)
check("调度设置可开", s["enabled"] is True and s["interval_seconds"] == 120)
try:
    sync.update_settings(True, 5, ADMIN)
    check("间隔下限被拒", False)
except ValueError:
    check("间隔下限被拒", True)

print()
if failures:
    print(f"失败 {len(failures)} 项：")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("全部断言通过 ✅")
