/* ============ 织云系统 前端 ============ */
"use strict";

const state = {
  token: localStorage.getItem("zy_token") || "",
  user: null,
  meta: null,
  businessLines: [],
  users: [],
  configMeta: null,
  envCatalog: [],
  filters: { business_line_id: "", owner_id: "", environment: "", status: "", q: "" },
  configFilters: { app_id: "", environment: "" },
  auditFilters: { app_id: "", business_line_id: "", environment: "", action: "", start: "", end: "" },
  opsFilters: { app_id: "", business_line_id: "", environment: "", category: "", start: "", end: "" },
};

/* ---------------- 工具 ---------------- */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function toast(msg, type) {
  const el = document.createElement("div");
  el.className = "toast" + (type ? " " + type : "");
  el.textContent = msg;
  $("#toast-root").appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtAgo(ts) {
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)} 天前`;
  return fmtTime(ts).slice(0, 10);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: {
      "Content-Type": "application/json",
      ...(state.token ? { "X-Token": state.token } : {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) {
    // detail 可能是字符串，也可能是 {message, ...冲突载荷}
    const detail = data && data.detail;
    const message = typeof detail === "string"
      ? detail
      : (detail && detail.message) || `请求失败（HTTP ${res.status}）`;
    if (res.status === 401) { logout(false); }
    const error = new Error(message);
    error.status = res.status;
    error.payload = (detail && typeof detail === "object") ? detail : null;
    throw error;
  }
  return data;
}

function statusBadge(app) {
  return `<span class="badge ${esc(app.status)}">${esc(app.status_label)}</span>`;
}

function envLabel(key) {
  const e = (state.envCatalog || []).find((x) => x.value === key);
  if (e) return e.label;
  const m = (state.meta && state.meta.environments || []).find((x) => x.value === key);
  return m ? m.label : key;
}

function envTagHtml(key, label) {
  return `<span class="env-tag ${key === "prod" ? "prod" : ""}">${esc(label || envLabel(key))}</span>`;
}

function paintWindowBar(w, canEdit) {
  if (!w) return "";
  if (!w.restricted) {
    return `<div class="window-bar open"><span>🟢 全时段允许发布</span><span class="wb-detail">该环境未限制发布时间</span></div>`;
  }
  if (w.allowed) {
    return `<div class="window-bar open">
      <span>🟢 发布窗口开放中</span>
      <span class="wb-detail">${esc(w.window_text)}${w.next_close_at ? ` · 今天 ${fmtClock(w.next_close_at)} 关闭` : ""}</span>
    </div>`;
  }
  return `<div class="window-bar closed">
    <span>🚫 ${esc(w.message)}</span>
    ${w.next_open_label ? `<span class="wb-next">下次可发布：${esc(w.next_open_label)}</span>` : ""}
  </div>`;
}

function envTag(app) {
  return `<span class="env-tag ${esc(app.environment)}">${esc(app.environment_label)}</span>`;
}

function redDotsHtml(app) {
  if (!app.red_dots || !app.red_dots.length) return "";
  return app.red_dots.map((d) => `<span class="red-dot">${esc(d.label)}</span>`).join(" ");
}

/* ---------------- 登录 ---------------- */
async function showLogin() {
  $("#topnav").hidden = true;
  $("#user-chip").hidden = true;
  $("#view").innerHTML = "";
  const overlay = $("#login-overlay");
  overlay.hidden = false;
  const box = $("#login-users");
  box.innerHTML = `<div class="empty-tip">加载账号中…</div>`;
  try {
    const users = await api("/api/public/users");
    box.innerHTML = users.map((u) => `
      <div class="login-user" data-username="${esc(u.username)}">
        <div class="u-name">${esc(u.name)}</div>
        <div class="u-meta">${esc(u.business_line_name || "平台全局")}</div>
        <span class="u-role role-${esc(u.role)}">${esc(u.role_label || u.role)}</span>
      </div>`).join("");
    $$(".login-user", box).forEach((el) => {
      el.onclick = () => doLogin(el.dataset.username);
    });
  } catch (e) {
    box.innerHTML = `<div class="empty-tip">账号加载失败：${esc(e.message)}</div>`;
  }
}

async function doLogin(username) {
  try {
    const data = await api("/api/login", { method: "POST", body: { username } });
    state.token = data.token;
    state.user = data.user;
    localStorage.setItem("zy_token", data.token);
    $("#login-overlay").hidden = true;
    await bootstrap();
    toast(`欢迎，${data.user.name}`, "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

function logout(showTip = true) {
  state.token = "";
  state.user = null;
  localStorage.removeItem("zy_token");
  if (showTip) toast("已退出登录");
  showLogin();
}

/* ---------------- 启动 ---------------- */
async function bootstrap() {
  try {
    if (!state.user) state.user = await api("/api/me");
    const [meta, bls, usrs, cfgMeta, catalog] = await Promise.all([
      api("/api/meta"), api("/api/business-lines"), api("/api/users"),
      api("/api/config/meta"), api("/api/environments/catalog"),
    ]);
    state.meta = meta;
    state.businessLines = bls;
    state.users = usrs;
    state.configMeta = cfgMeta;
    state.envCatalog = catalog;
  } catch (e) {
    if (e.status !== 401) toast(e.message, "error");
    return;
  }
  $("#topnav").hidden = false;
  // 数据同步是平台级能力：非管理员直接隐藏入口（接口侧同样强制 403）
  $$("#topnav [data-admin-only]").forEach((a) => { a.hidden = state.user.role !== "admin"; });
  const chip = $("#user-chip");
  chip.hidden = false;
  const p = state.user.permissions || {};
  const scopeTxt = state.user.role === "admin"
    ? "全部业务线 · 全部环境"
    : (p.scopes || []).map((s) => `${s.business_line_name}·${s.environment_label}`).join("、")
      || (state.user.business_line_name || "无可见范围");
  chip.innerHTML = `
    <span class="chip-main">
      <span class="u-role role-${esc(state.user.role)}">${esc(p.role_label || state.user.role)}</span>
      ${esc(state.user.name)}
    </span>
    <span class="chip-scope" title="${esc(scopeTxt)}">${esc(scopeTxt)}</span>
    <button class="logout" id="btn-logout">退出</button>`;
  $("#btn-logout").onclick = () => logout();
  route();
}

function route() {
  if (!state.user) return;
  const hash = location.hash || "#/console";
  const parts = hash.replace(/^#\//, "").split("/");
  $$("#topnav a").forEach((a) => a.classList.toggle("active", a.dataset.route === parts[0]));
  if (parts[0] === "apps" && parts[1]) renderAppDetail(parts[1]);
  else if (parts[0] === "apps") renderApps();
  else if (parts[0] === "health") {
    if (parts[1]) renderAppHealth(parts[1], parts[2] || "");
    else renderHealthBoard();
  } else if (parts[0] === "config") {
    if (parts[1]) renderConfigProfile(parts[1], parts[2] || "");
    else renderConfigList();
  } else if (parts[0] === "audit") renderAudit();
  else if (parts[0] === "sync") renderSync();
  else if (parts[0] === "admin") renderAdmin();
  else renderConsole();
}

window.addEventListener("hashchange", () => { if (state.user) route(); });

/* ---------------- 资产控制台 ---------------- */
async function renderConsole() {
  const view = $("#view");
  view.innerHTML = `<div class="empty-tip">加载中…</div>`;
  let data;
  try {
    data = await api("/api/console/summary");
  } catch (e) {
    view.innerHTML = errorStateHtml("加载失败", e.message);
    return;
  }
  const t = data.totals;
  const ha = data.health_alerts || { offline_instances: [], restart_envs: [], totals: {} };
  const maxTotal = Math.max(1, ...data.by_business_line.map((b) => b.total));

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>资产控制台</h2>
        <div class="sub">${state.user.role === "admin" ? "全部业务线" : "按你的可见范围（业务线 × 环境）"}应用资产概览</div>
      </div>
    </div>

    <div class="stats-grid">
      <div class="panel stat-card"><div class="num">${t.apps}</div><div class="label">应用总数</div></div>
      <div class="panel stat-card"><div class="num">${t.business_lines}</div><div class="label">业务线</div></div>
      <div class="panel stat-card"><div class="num">${t.recent_changed}</div><div class="label">近 7 天有变更</div></div>
      <div class="panel stat-card"><div class="num ${t.red_dot_apps ? "red" : ""}">${t.red_dot_apps}</div><div class="label">红点待处理应用</div></div>
    </div>

    <div class="panel panel-pad alert-panel" style="margin-bottom:16px">
      <h3>🚨 实例健康告警
        <span style="font-weight:400;font-size:12px;color:var(--ink-3)">掉线实例与频繁重启环境，无需逐个应用翻找</span>
      </h3>
      ${ha.totals.offline_instances === 0 && ha.totals.restart_envs === 0
        ? '<div class="empty-tip">当前可见范围内没有掉线实例与异常重启，运行平稳。</div>'
        : `<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px" id="ha-grid">
          <div>
            <p style="margin:0 0 6px"><span class="red-dot">实例掉线（${ha.totals.offline_instances}）</span></p>
            <div id="ha-offline">
              ${ha.offline_instances.length ? ha.offline_instances.map((x) => `
                <div class="alert-item" data-app="${x.app_id}" data-env="${esc(x.environment)}">
                  <span class="dot offline"></span>
                  <span class="ai-target">${esc(x.instance_name)}</span>
                  <span class="ai-meta">${esc(x.business_line_name)} · ${esc(x.app_name)} · ${esc(x.environment_label)}</span>
                  <span class="ai-time">掉线 ${fmtAgo(x.offline_since)}</span>
                </div>`).join("") : '<div class="empty-tip" style="padding:10px 0">无掉线实例</div>'}
            </div>
          </div>
          <div>
            <p style="margin:0 0 6px"><b style="color:var(--ink-2)">频繁/偏多重启环境（${ha.totals.restart_envs}）</b>
              <span style="font-size:11px;color:var(--ink-3)">按近 24h / 7 天频率分级</span></p>
            <div id="ha-restart">
              ${ha.restart_envs.length ? ha.restart_envs.map((x) => `
                <div class="alert-item" data-app="${x.app_id}" data-env="${esc(x.environment)}">
                  <span class="hl ${x.level}">${esc(x.level_label)}</span>
                  <span class="ai-target">${esc(x.app_name)} · ${esc(x.environment_label)}</span>
                  <span class="ai-meta">${esc(x.business_line_name)}</span>
                  <span class="ai-time">近 24h
                    <span class="${x.level === "critical" ? "restart-badge-crit" : "restart-badge-warn"}">${x.restarts_24h} 次</span>
                    ${x.last_restart_at ? `· 最近 ${fmtAgo(x.last_restart_at)}` : ""}</span>
                </div>`).join("") : '<div class="empty-tip" style="padding:10px 0">无异常重启环境</div>'}
            </div>
          </div>
        </div>`}
    </div>

    <div class="dash-grid">
      <div class="panel panel-pad">
        <h3>各业务线应用数分布</h3>
        ${data.by_business_line.map((b) => `
          <div class="bl-row">
            <span class="bl-name" title="${esc(b.name)}">${esc(b.name)}</span>
            <div class="bl-bar-track">
              ${["developing", "online", "maintenance", "offline"].map((s) =>
                b[s] ? `<div class="bl-bar-seg ${s}" style="width:${(b[s] / maxTotal) * 100}%" title="${s}: ${b[s]}"></div>` : "").join("")}
            </div>
            <span class="bl-count">${b.total} 个应用</span>
          </div>`).join("")}
        <div class="legend">
          <span><i style="background:#6a9bff"></i>在研</span>
          <span><i style="background:#34c77b"></i>上线</span>
          <span><i style="background:#f0b429"></i>维保</span>
          <span><i style="background:#c3cad6"></i>下线</span>
        </div>
      </div>

      <div class="panel panel-pad">
        <h3>近 7 天有变更的应用</h3>
        <div class="mini-list">
          ${data.recent_changed_apps.length ? data.recent_changed_apps.map((a) => `
            <div class="mini-item" data-app-id="${a.id}">
              <span class="mini-title">${esc(a.name)}</span>
              <span class="badge ${esc(a.status)}">${esc({ developing: "在研", online: "上线", maintenance: "维保", offline: "下线" }[a.status])}</span>
              <span class="mini-meta">${esc(a.business_line_name)} · ${a.change_count} 次变更 · ${fmtAgo(a.last_changed_at)}</span>
            </div>`).join("") : `<div class="empty-tip">近 7 天暂无变更</div>`}
        </div>
      </div>

      <div class="panel panel-pad span-full">
        <h3>红点提醒（环境变量缺失 / 缺失负责人）</h3>
        <div class="red-groups">
          <div>
            <p><span class="red-dot">缺失负责人（${data.red_dots.missing_owner.length}）</span></p>
            <div class="chip-list">
              ${data.red_dots.missing_owner.length
                ? data.red_dots.missing_owner.map((a) => `<span class="app-chip" data-app-id="${a.id}">${esc(a.name)} · ${esc(a.business_line_name)}</span>`).join("")
                : `<span class="empty-tip">无</span>`}
            </div>
          </div>
          <div>
            <p><span class="red-dot">环境变量缺失（${data.red_dots.missing_env.length}）</span></p>
            <div class="chip-list">
              ${data.red_dots.missing_env.length
                ? data.red_dots.missing_env.map((a) => `<span class="app-chip" data-app-id="${a.id}">${esc(a.name)} · ${esc(a.business_line_name)}</span>`).join("")
                : `<span class="empty-tip">无</span>`}
            </div>
          </div>
        </div>
      </div>
    </div>`;

  $$("[data-app-id]", view).forEach((el) => {
    el.onclick = () => { location.hash = `#/apps/${el.dataset.appId}`; };
  });
  $$("#ha-grid [data-app]", view).forEach((el) => {
    el.onclick = () => { location.hash = `#/health/${el.dataset.app}/${el.dataset.env}`; };
  });
}

/* ---------------- 应用台账 ---------------- */
async function renderApps() {
  const view = $("#view");
  const f = state.filters;
  const isAdmin = state.user.role === "admin";
  // 新建应用：平台管理员或业务线负责人；应用负责人/观察者看不到入口
  const canCreateApp = isAdmin || state.user.role === "bl_owner";
  const scopeDesc = isAdmin
    ? "全部业务线"
    : (state.user.permissions.scopes || []).length
      ? "按授权的业务线 × 环境收窄"
      : (state.user.business_line_name || "仅本人负责的应用");

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>应用台账</h2>
        <div class="sub">${esc(scopeDesc)} · 支持业务线 / 负责人 / 环境 / 状态组合筛选</div>
      </div>
      ${canCreateApp ? '<button class="btn primary" id="btn-new-app">+ 新建应用</button>' : ""}
    </div>
    <div class="panel filter-bar">
      <select id="f-bl" ${isAdmin ? "" : "disabled"}>
        <option value="">可见的全部业务线</option>
        ${state.businessLines.map((b) => `<option value="${b.id}" ${String(b.id) === String(f.business_line_id) ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
      </select>
      <select id="f-owner">
        <option value="">全部负责人</option>
        ${state.users.map((u) => `<option value="${u.id}" ${String(u.id) === String(f.owner_id) ? "selected" : ""}>${esc(u.name)}（${esc(u.business_line_name || "平台")}）</option>`).join("")}
      </select>
      <select id="f-env">
        <option value="">全部环境</option>
        ${state.envCatalog.map((e) => `<option value="${e.value}" ${f.environment === e.value ? "selected" : ""}>${esc(e.label)}</option>`).join("")}
      </select>
      <select id="f-status">
        <option value="">全部状态</option>
        ${state.meta.statuses.map((s) => `<option value="${s.value}" ${f.status === s.value ? "selected" : ""}>${esc(s.label)}</option>`).join("")}
      </select>
      <input id="f-q" class="grow" placeholder="搜索应用名…" value="${esc(f.q)}">
      <button class="btn" id="btn-reset">重置</button>
    </div>
    <div id="apps-result"><div class="empty-tip">加载中…</div></div>`;

  const newBtn = $("#btn-new-app");
  if (newBtn) newBtn.onclick = () => openAppModal();
  $("#btn-reset").onclick = () => {
    state.filters = { business_line_id: "", owner_id: "", environment: "", status: "", q: "" };
    renderApps();
  };
  const reload = () => {
    state.filters = {
      business_line_id: $("#f-bl").value,
      owner_id: $("#f-owner").value,
      environment: $("#f-env").value,
      status: $("#f-status").value,
      q: $("#f-q").value.trim(),
    };
    loadAppList();
  };
  ["#f-bl", "#f-owner", "#f-env", "#f-status"].forEach((sel) => { $(sel).onchange = reload; });
  let timer = null;
  $("#f-q").oninput = () => { clearTimeout(timer); timer = setTimeout(reload, 300); };
  $("#f-q").onkeydown = (e) => { if (e.key === "Enter") reload(); };

  await loadAppList();
}

async function loadAppList() {
  const box = $("#apps-result");
  const p = new URLSearchParams();
  const f = state.filters;
  if (f.business_line_id) p.set("business_line_id", f.business_line_id);
  if (f.owner_id) p.set("owner_id", f.owner_id);
  if (f.environment) p.set("environment", f.environment);
  if (f.status) p.set("status", f.status);
  if (f.q) p.set("q", f.q);
  let apps;
  try {
    apps = await api(`/api/apps?${p.toString()}`);
  } catch (e) {
    box.innerHTML = errorStateHtml(e.status === 403 ? "403 无权访问" : "加载失败", e.message);
    return;
  }
  if (!apps.length) {
    box.innerHTML = `<div class="panel panel-pad empty-tip">没有符合条件的应用</div>`;
    return;
  }

  box.innerHTML = `
    <div class="panel table-wrap">
      <table class="app-table">
        <thead><tr>
          <th>应用</th><th>业务线</th><th>负责人</th><th>集群</th><th>环境</th><th>状态</th><th>风险提示</th><th>更新时间</th>
        </tr></thead>
        <tbody>
          ${apps.map((a) => `
            <tr data-app-id="${a.id}">
              <td class="app-name-cell">${esc(a.name)}<div class="app-desc">${esc(a.description || "")}</div></td>
              <td>${esc(a.business_line_name)}</td>
              <td>${a.owner_name ? esc(a.owner_name) : '<span class="red-dot">未设置</span>'}</td>
              <td>${esc(a.cluster)}</td>
              <td>${envTag(a)}</td>
              <td>${statusBadge(a)}</td>
              <td>${redDotsHtml(a) || '<span style="color:var(--ink-3)">—</span>'}</td>
              <td title="${fmtTime(a.updated_at)}">${fmtAgo(a.updated_at)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>
    <div class="card-list">
      ${apps.map((a) => `
        <div class="panel app-card" data-app-id="${a.id}">
          <div class="card-head">
            <span class="name">${esc(a.name)}</span>
            ${envTag(a)}
            ${statusBadge(a)}
          </div>
          <div class="card-fields">
            <div class="cf"><span class="k">业务线</span>${esc(a.business_line_name)}</div>
            <div class="cf"><span class="k">负责人</span>${a.owner_name ? esc(a.owner_name) : "未设置"}</div>
            <div class="cf"><span class="k">集群</span>${esc(a.cluster)}</div>
            <div class="cf"><span class="k">更新</span>${fmtAgo(a.updated_at)}</div>
          </div>
          ${a.red_dots.length ? `<div class="card-red">${redDotsHtml(a)}</div>` : ""}
        </div>`).join("")}
    </div>`;

  $$("[data-app-id]", box).forEach((el) => {
    el.onclick = () => { location.hash = `#/apps/${el.dataset.appId}`; };
  });
}

/* ---------------- 应用详情 ---------------- */
const STATUS_FLOW = ["developing", "online", "maintenance", "offline"];

async function renderAppDetail(appId) {
  const view = $("#view");
  view.innerHTML = `<div class="empty-tip">加载中…</div>`;
  let app;
  try {
    app = await api(`/api/apps/${appId}`);
  } catch (e) {
    // 越权 / 不存在：明确错误态，而不是空白页
    view.innerHTML = errorStateHtml(
      e.status === 403 ? "403 无权访问" : e.status === 404 ? "404 应用不存在" : "加载失败",
      e.message
    );
    return;
  }
  const isOffline = app.status === "offline";
  const curIdx = STATUS_FLOW.indexOf(app.status);
  const nextStatuses = STATUS_FLOW.slice(curIdx + 1);
  const canManage = !!app.can_manage;
  const canTransfer = !!app.can_transfer;

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>${esc(app.name)} ${statusBadge(app)}</h2>
        <div class="sub">${esc(app.business_line_name)} · ${esc(app.cluster)} · ${esc(app.environment_label)}环境</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn" id="btn-back">← 返回台账</button>
        <button class="btn" id="btn-health">应用健康</button>
        ${canManage && !isOffline ? '<button class="btn" id="btn-env-mgr">环境与窗口</button>' : ""}
        <button class="btn" id="btn-config">配置档案</button>
        ${canTransfer ? '<button class="btn" id="btn-transfer">应用交接</button>' : ""}
        ${canManage && !isOffline ? '<button class="btn" id="btn-edit">编辑信息</button>' : ""}
      </div>
    </div>

    ${!canManage ? `<div class="panel panel-pad perm-notice">
      你当前以<b>只读方式</b>查看该应用：${esc(state.user.permissions.role_label)}
      无权修改其台账信息、生命周期与环境变量；需要变更请联系该应用负责人或所属业务线负责人。
    </div>` : ""}

    ${app.red_dots.length ? `<div class="panel panel-pad" style="margin-bottom:16px">
      <span style="margin-right:10px;color:var(--ink-2)">风险提示：</span>${redDotsHtml(app)}
    </div>` : ""}

    <div class="detail-grid">
      <div>
        <div class="panel panel-pad" style="margin-bottom:16px">
          <h3>生命周期</h3>
          <div class="lifecycle">
            ${STATUS_FLOW.map((s, i) => `
              ${i > 0 ? `<div class="lc-line ${i <= curIdx ? "done" : ""}"></div>` : ""}
              <div class="lc-step ${i < curIdx ? "done" : ""} ${i === curIdx ? "current" : ""}">
                <span class="lc-dot">${i + 1}</span>
                <span class="lc-label">${esc(state.meta.statuses[i].label)}</span>
              </div>`).join("")}
          </div>
          ${isOffline
            ? `<p style="color:var(--ink-3);font-size:13px">应用已下线（终态），所有信息只读，不能再变更。</p>`
            : (canManage ? `<div class="status-actions">
                <span style="color:var(--ink-2);font-size:13px;align-self:center">流转到：</span>
                ${nextStatuses.map((s) => {
                  const label = state.meta.statuses.find((x) => x.value === s).label;
                  return `<button class="btn small ${s === "offline" ? "danger" : "primary"}" data-to-status="${s}">${esc(label)}</button>`;
                }).join("")}
              </div>` : `<p style="color:var(--ink-3);font-size:13px">你无权变更该应用的生命周期状态。</p>`)}
        </div>

        <div class="panel panel-pad" style="margin-bottom:16px">
          <h3>基本信息</h3>
          <div class="kv-grid">
            <div class="kv"><div class="k">应用 ID</div><div class="v">#${app.id}</div></div>
            <div class="kv"><div class="k">所属业务线</div><div class="v">${esc(app.business_line_name)}</div></div>
            <div class="kv"><div class="k">负责人</div><div class="v">${app.owner_name ? esc(app.owner_name) : '<span class="red-dot">缺失负责人</span>'}</div></div>
            <div class="kv"><div class="k">所属集群</div><div class="v">${esc(app.cluster)}</div></div>
            <div class="kv"><div class="k">环境</div><div class="v">${envTag(app)}</div></div>
            <div class="kv"><div class="k">创建时间</div><div class="v">${fmtTime(app.created_at)}</div></div>
            <div class="kv" style="grid-column:1/-1"><div class="k">描述</div><div class="v">${esc(app.description || "—")}</div></div>
          </div>
        </div>

        <div class="panel panel-pad">
          <h3>环境变量 ${app.env_vars.length === 0 ? '<span class="red-dot">环境变量缺失</span>' : `<span style="color:var(--ink-3);font-weight:400;font-size:12px">（${app.env_vars.length} 项）</span>`}</h3>
          <div id="env-editor"></div>
        </div>
      </div>

      <div>
        <div class="panel panel-pad">
          <h3>变更记录</h3>
          <div class="log-list">
            ${app.change_logs.length ? app.change_logs.map((l) => `
              <div class="log-item">
                <div><b>${esc(l.action)}</b> · ${esc(l.detail)}</div>
                <div class="log-meta">${esc(l.user_name || "系统")} · ${fmtTime(l.created_at)}</div>
              </div>`).join("") : `<div class="empty-tip">暂无变更记录</div>`}
          </div>
        </div>
        <div class="panel panel-pad" style="margin-top:16px">
          <h3>应用交接记录</h3>
          <div class="log-list">
            ${(app.transfers || []).length ? app.transfers.map((t) => `
              <div class="log-item transfer-item">
                <div><b>交接：</b>${esc(t.old_owner_name || "（空缺）")} → ${esc(t.new_owner_name)}</div>
                <div class="log-meta">由 ${esc(t.transfer_by_name || "系统")} 发起 · ${fmtTime(t.created_at)}</div>
                ${t.note ? `<div class="transfer-note">${esc(t.note)}</div>` : ""}
                <div class="log-meta">配置项、历史版本与密文授权责任随应用一并移交</div>
              </div>`).join("") : `<div class="empty-tip">暂无交接记录</div>`}
          </div>
        </div>
      </div>
    </div>`;

  $("#btn-back").onclick = () => { location.hash = "#/apps"; };
  $("#btn-health").onclick = () => { location.hash = `#/health/${app.id}`; };
  const envMgrBtn = $("#btn-env-mgr");
  if (envMgrBtn) envMgrBtn.onclick = () => openEnvManagerModal(app);
  $("#btn-config").onclick = () => { location.hash = `#/config/${app.id}/${app.environment}`; };
  const transferBtn = $("#btn-transfer");
  if (transferBtn) transferBtn.onclick = () => openTransferModal(app);
  const editBtn = $("#btn-edit");
  if (editBtn) editBtn.onclick = () => openAppModal(app);
  $$("[data-to-status]", view).forEach((btn) => {
    btn.onclick = () => transitionStatus(app.id, btn.dataset.toStatus);
  });
  renderEnvEditor(app);
}

function renderEnvEditor(app) {
  const box = $("#env-editor");
  const offline = app.status === "offline";
  const noPerm = !app.can_manage;
  const readOnly = offline || noPerm;
  const rows = app.env_vars.map((v) => ({ ...v }));
  if (!rows.length) rows.push({ key: "", value: "" });

  box.innerHTML = `
    <table class="env-table">
      <thead><tr><th style="width:38%">KEY</th><th>VALUE</th>${readOnly ? "" : '<th style="width:52px"></th>'}</tr></thead>
      <tbody id="env-tbody"></tbody>
    </table>
    ${offline
      ? `<p style="color:var(--ink-3);font-size:13px">应用已下线（终态），环境变量只读。</p>`
      : (noPerm
        ? `<p style="color:var(--ink-3);font-size:13px">你没有该应用的编辑权（当前角色：${esc(state.user.permissions.role_label)}），环境变量只读。</p>`
        : `<div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn small" id="env-add">+ 添加一行</button>
          <button class="btn primary small" id="env-save">保存环境变量</button>
        </div>`)}`;

  const tbody = $("#env-tbody", box);
  function paint() {
    tbody.innerHTML = rows.map((r, i) => `
      <tr>
        <td><input data-i="${i}" data-f="key" placeholder="如 DB_HOST" value="${esc(r.key)}" ${readOnly ? "disabled" : ""}></td>
        <td><input data-i="${i}" data-f="value" placeholder="值" value="${esc(r.value)}" ${readOnly ? "disabled" : ""}></td>
        ${readOnly ? "" : `<td><button class="btn small danger" data-del="${i}">删</button></td>`}
      </tr>`).join("");
    $$("input", tbody).forEach((inp) => {
      inp.oninput = () => { rows[+inp.dataset.i][inp.dataset.f] = inp.value; };
    });
    $$("[data-del]", tbody).forEach((btn) => {
      btn.onclick = () => { rows.splice(+btn.dataset.del, 1); if (!rows.length) rows.push({ key: "", value: "" }); paint(); };
    });
  }
  paint();

  if (!readOnly) {
    $("#env-add", box).onclick = () => { rows.push({ key: "", value: "" }); paint(); };
    $("#env-save", box).onclick = async () => {
      const vars = rows.filter((r) => r.key.trim()).map((r) => ({ key: r.key.trim(), value: r.value }));
      try {
        await api(`/api/apps/${app.id}/env-vars`, { method: "PUT", body: { vars } });
        toast("环境变量已保存", "success");
        renderAppDetail(app.id);
      } catch (e) { toast(e.message, "error"); }
    };
  }
}

async function transitionStatus(appId, toStatus) {
  const label = state.meta.statuses.find((s) => s.value === toStatus).label;
  if (toStatus === "offline" && !confirm(`确认将应用流转到「${label}」？下线为终态，不可再变更。`)) return;
  try {
    await api(`/api/apps/${appId}/status`, { method: "POST", body: { status: toStatus } });
    toast(`已流转到「${label}」`, "success");
  } catch (e) {
    // 非法状态回退等：展示后端给出的具体原因
    toast(e.message, "error");
  }
  renderAppDetail(appId);
}

/* ---------------- 新建 / 编辑弹窗 ---------------- */
function openAppModal(app) {
  const isEdit = !!app;
  const isAdmin = state.user.role === "admin";
  const root = $("#modal-root");
  const ownerOptions = (blId) => state.users
    .filter((u) => !blId || String(u.business_line_id) === String(blId))
    .map((u) => `<option value="${u.id}" ${app && app.owner_id === u.id ? "selected" : ""}>${esc(u.name)}</option>`).join("");

  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal">
        <h3>${isEdit ? `编辑应用 #${app.id}` : "新建应用"}</h3>
        <div class="form-grid">
          <div>
            <label>应用名称 *</label>
            <input id="m-name" maxlength="64" value="${isEdit ? esc(app.name) : ""}" placeholder="同一业务线下不可重名">
          </div>
          <div>
            <label>所属业务线 *</label>
            <select id="m-bl" ${isEdit || !isAdmin ? "disabled" : ""}>
              ${state.businessLines.map((b) => `<option value="${b.id}" ${isEdit && app.business_line_id === b.id ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
            </select>
          </div>
          <div>
            <label>负责人</label>
            <select id="m-owner" ${isEdit ? "disabled" : ""}><option value="">（暂不指定）</option>${ownerOptions(isEdit ? app.business_line_id : state.businessLines[0].id)}</select>
            ${isEdit ? '<div class="field-hint">归属变更（换负责人）属于交接，请在详情页点「应用交接」并留痕</div>' : ""}
          </div>
          <div>
            <label>所属集群 *</label>
            <select id="m-cluster">
              ${state.meta.clusters.map((c) => `<option ${isEdit && app.cluster === c ? "selected" : ""}>${esc(c)}</option>`).join("")}
            </select>
          </div>
          <div>
            <label>环境 *</label>
            <select id="m-env" ${isEdit ? "" : ""}>
              ${(isEdit ? [] : state.meta.environments).map((e) => `<option value="${e.value}">${esc(e.label)}</option>`).join("")}
              ${isEdit ? `<option value="${esc(app.environment)}">${esc(app.environment_label)}</option>` : ""}
            </select>
            ${isEdit ? '<div class="field-hint">可选环境以本应用已注册的为准；新增环境请到「环境与窗口」</div>' : '<div class="field-hint">新建后自动开通开发/测试/预发/生产四个环境</div>'}
          </div>
          <div class="full">
            <label>描述</label>
            <textarea id="m-desc" rows="2" placeholder="应用用途简述">${isEdit ? esc(app.description || "") : ""}</textarea>
          </div>
        </div>
        <div class="form-error" id="m-error"></div>
        <div class="form-actions">
          <button class="btn" id="m-cancel">取消</button>
          <button class="btn primary" id="m-submit">${isEdit ? "保存" : "创建"}</button>
        </div>
      </div>
    </div>`;

  const close = () => { root.innerHTML = ""; };
  $("#m-cancel").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };

  const blSel = $("#m-bl");
  if (!isEdit) {
    blSel.onchange = () => { $("#m-owner").innerHTML = `<option value="">（暂不指定）</option>` + ownerOptions(blSel.value); };
  }
  if (isEdit) {
    // 编辑时主环境可选范围 = 本应用已注册环境（含自定义环境）
    api(`/api/apps/${app.id}/environments`).then((envs) => {
      const sel = $("#m-env");
      if (!sel) return;
      sel.innerHTML = envs.map((e) =>
        `<option value="${esc(e.env_key)}" ${e.env_key === app.environment ? "selected" : ""}>${esc(e.env_label)}${e.is_builtin ? "" : "（自定义）"}</option>`
      ).join("");
    }).catch(() => {});
  }

  $("#m-submit").onclick = async () => {
    const errBox = $("#m-error");
    errBox.classList.remove("show");
    const name = $("#m-name").value.trim();
    if (!name) { errBox.textContent = "应用名称不能为空"; errBox.classList.add("show"); return; }
    try {
      if (isEdit) {
        await api(`/api/apps/${app.id}`, {
          method: "PATCH",
          body: {
            name,
            set_owner: true,
            owner_id: $("#m-owner").value ? +$("#m-owner").value : null,
            cluster: $("#m-cluster").value,
            environment: $("#m-env").value,
            description: $("#m-desc").value.trim(),
          },
        });
        toast("应用信息已更新", "success");
        close();
        renderAppDetail(app.id);
      } else {
        await api("/api/apps", {
          method: "POST",
          body: {
            name,
            business_line_id: +blSel.value,
            owner_id: $("#m-owner").value ? +$("#m-owner").value : null,
            cluster: $("#m-cluster").value,
            environment: $("#m-env").value,
            description: $("#m-desc").value.trim(),
          },
        });
        toast(`应用「${name}」创建成功（初始状态：在研）`, "success");
        close();
        renderApps();
      }
    } catch (e) {
      // 重名 409 / 校验失败等：表单内展示原因
      errBox.textContent = e.message;
      errBox.classList.add("show");
    }
  };
}

/* ---------------- 配置档案 · 列表 ---------------- */
function cfgTypeLabel(v) {
  const t = state.configMeta.types.find((x) => x.value === v);
  return t ? t.label : v;
}
function cfgScopeLabel(v) {
  const t = state.configMeta.scopes.find((x) => x.value === v);
  return t ? t.label : v;
}
function actionLabel(a) {
  const t = state.configMeta.actions.find((x) => x.value === a);
  return t ? t.label : a;
}

async function renderConfigList() {
  const view = $("#view");
  const f = state.configFilters;
  const isAdmin = state.user.role === "admin";
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>配置档案</h2>
        <div class="sub">配置项按「应用 + 环境」管理：键、值、类型、生效范围、密文；密文默认脱敏</div>
      </div>
    </div>
    <div class="panel filter-bar">
      <select id="cf-bl" ${isAdmin ? "" : "disabled"}>
        <option value="">全部业务线</option>
        ${state.businessLines.map((b) => `<option value="${b.id}" ${String(b.id) === String(f.business_line_id) ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
      </select>
      <select id="cf-env">
        <option value="">全部环境</option>
        ${state.envCatalog.map((e) => `<option value="${e.value}" ${f.environment === e.value ? "selected" : ""}>${esc(e.label)}</option>`).join("")}
      </select>
      <button class="btn" id="cf-reset">重置</button>
    </div>
    <div id="cfg-result"><div class="empty-tip">加载中…</div></div>`;

  const load = async () => {
    const p = new URLSearchParams();
    if (isAdmin && $("#cf-bl").value) p.set("business_line_id", $("#cf-bl").value);
    if ($("#cf-env").value) p.set("environment", $("#cf-env").value);
    try {
      const rows = await api(`/api/config/profiles?${p.toString()}`);
      const box = $("#cfg-result");
      if (!rows.length) {
        box.innerHTML = `<div class="panel panel-pad empty-tip">还没有配置档案，进入应用详情对应的环境后可新增配置</div>`;
        return;
      }
      box.innerHTML = `
        <div class="panel table-wrap">
          <table class="app-table">
            <thead><tr><th>应用</th><th>业务线</th><th>环境</th><th>配置项</th><th>密文</th><th>最新版本</th><th>最近更新</th></tr></thead>
            <tbody>
              ${rows.map((r) => `
                <tr data-app="${r.app_id}" data-env="${r.environment}">
                  <td class="app-name-cell">${esc(r.app_name)}
                    ${r.app_status === "offline" ? '<span class="badge offline">已下线</span>' : ""}</td>
                  <td>${esc(r.business_line_name)}</td>
                  <td><span class="env-tag ${esc(r.environment)}">${esc(envLabel(r.environment))}</span></td>
                  <td>${r.item_count} 项</td>
                  <td>${r.secret_count ? `<span class="secret-chip">🔒 ${r.secret_count}</span>` : '<span style="color:var(--ink-3)">—</span>'}</td>
                  <td><b>v${r.latest_version}</b>${r.latest_note ? `<div class="app-desc">${esc(r.latest_note)}</div>` : ""}</td>
                  <td title="${fmtTime(r.latest_at)}">${esc(r.created_by_name || "系统")} · ${fmtAgo(r.latest_at)}</td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>`;
      $$("[data-app]", box).forEach((tr) => {
        tr.onclick = () => { location.hash = `#/config/${tr.dataset.app}/${tr.dataset.env}`; };
      });
    } catch (e) {
      $("#cfg-result").innerHTML = errorStateHtml("加载失败", e.message);
    }
  };
  $("#cf-bl").onchange = load;
  $("#cf-env").onchange = load;
  $("#cf-reset").onclick = () => {
    state.configFilters = { business_line_id: "", environment: "" };
    renderConfigList();
  };
  await load();
}

/* ---------------- 配置档案 · 详情（单应用单环境） ---------------- */
async function renderConfigProfile(appId, environment) {
  const view = $("#view");
  if (!environment) {
    // 从菜单直接进 #/config/12：先取应用，默认落 prod（或应用所属环境）
    try {
      const a = await api(`/api/apps/${appId}`);
      location.hash = `#/config/${appId}/${a.environment}`;
    } catch (e) {
      view.innerHTML = errorStateHtml(e.status === 403 ? "403 无权访问" : "加载失败", e.message);
    }
    return;
  }
  await paintConfigProfile(appId, environment);
}

async function paintConfigProfile(appId, environment) {
  const view = $("#view");
  view.innerHTML = `<div class="empty-tip">加载中…</div>`;
  let data;
  try {
    data = await api(`/api/apps/${appId}/config?environment=${encodeURIComponent(environment)}`);
  } catch (e) {
    view.innerHTML = errorStateHtml(e.status === 403 ? "403 无权访问" : e.status === 404 ? "404 不存在" : "加载失败", e.message);
    return;
  }
  // 环境标签来自该应用已注册的环境（含自定义），不再用写死四枚举
  const envTabs = (data.env_permissions || []).map((m) => ({
    value: m.environment,
    label: m.environment_label || envLabel(m.environment),
  }));
  const permsEnv = {};
  (data.env_permissions || []).forEach((m) => { permsEnv[m.environment] = m; });
  const curPerm = data.permissions || { can_edit: false, can_reveal: false };
  const canEdit = !!curPerm.can_edit && !data.read_only;
  const canReveal = !!curPerm.can_reveal;

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>${esc(data.app_name)} · 配置档案</h2>
        <div class="sub">密文默认脱敏展示；查看明文需密文查看权并填写理由，服务端二次校验并留痕</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn" id="cp-back">← 档案列表</button>
        <button class="btn" id="cp-app">查看应用</button>
      </div>
    </div>
    <div class="env-tabs" id="cp-tabs">
      ${envTabs.map((e) => {
        const m = permsEnv[e.value];
        const denied = m && !m.visible;
        if (denied) {
          return `<span class="env-tab disabled ${e.value === "prod" ? "prod" : ""}"
             title="${esc(m.view_deny_reason || "该环境不在你的可见范围内")}"
             data-deny-env="${esc(e.environment_label || e.label)}">${esc(e.environment_label || e.label)} 🔒</span>`;
        }
        return `<a class="env-tab ${e.value === environment ? "active" : ""} ${e.value === "prod" ? "prod" : ""}"
         href="#/config/${appId}/${e.value}">${esc(e.environment_label || e.label)}</a>`;
      }).join("")}
    </div>

    ${data.window ? paintWindowBar(data.window, canEdit) : ""}

    ${data.read_only
      ? '<div class="panel panel-pad perm-notice">应用已下线（终态），配置档案只读，不能编辑或回滚。</div>'
      : (!curPerm.can_edit
        ? `<div class="panel panel-pad perm-notice warn">${esc(curPerm.edit_deny_reason || "你没有该环境的配置编辑权，当前为只读视图。")}</div>`
        : "")}
    ${!canReveal
      ? `<div class="panel panel-pad perm-notice">${esc(curPerm.reveal_deny_reason || "你没有该环境的密文查看权，密文只能看到脱敏值。")}</div>`
      : ""}

    <div class="detail-grid cfg-grid">
      <div>
        <div class="panel panel-pad" style="margin-bottom:16px">
          <div class="cfg-toolbar">
            <h3 style="margin:0">配置项（${data.items.length}）</h3>
            <div style="display:flex;gap:8px">
              <button class="btn small" id="cp-diff-btn">环境对比</button>
              ${canEdit ? '<button class="btn primary small" id="cp-edit-btn">编辑配置</button>' : ""}
            </div>
          </div>
          ${data.read_only ? '<p style="color:var(--ink-3);font-size:13px;margin:8px 0 0">应用已下线（终态），配置档案只读。</p>' : ""}
          <div id="cp-items" style="margin-top:12px"></div>
        </div>
        <div class="panel panel-pad" id="cp-diff-panel" hidden></div>
      </div>
      <div class="panel panel-pad">
        <h3>版本历史</h3>
        <div id="cp-versions" class="version-list"></div>
      </div>
    </div>`;

  $("#cp-back").onclick = () => { location.hash = "#/config"; };
  $("#cp-app").onclick = () => { location.hash = `#/apps/${appId}`; };
  const diffBtn = $("#cp-diff-btn");
  diffBtn.onclick = () => {
    const visibleEnvObjs = (data.env_permissions || [])
      .filter((m) => m.visible)
      .map((m) => ({ environment: m.environment, environment_label: m.environment_label || envLabel(m.environment) }));
    openEnvDiffModal(appId, environment, visibleEnvObjs);
  };
  const editBtn = $("#cp-edit-btn");
  if (editBtn) editBtn.onclick = () => openConfigEditor(appId, environment, data);
  $$("[data-deny-env]", view).forEach((el) => {
    el.onclick = () => toast(el.title || `你没有「${el.dataset.denyEnv}」环境的访问权`, "error");
  });

  paintConfigItems(data.items, canReveal, curPerm.reveal_deny_reason);
  paintVersions(appId, environment, data.versions, data.read_only, canEdit);
}

function paintConfigItems(items, canReveal, revealDenyReason) {
  const box = $("#cp-items");
  if (!items.length) {
    box.innerHTML = `<div class="empty-tip">该环境暂无配置项</div>`;
    return;
  }
  box.innerHTML = `
    <table class="env-table cfg-table">
      <thead><tr><th>键</th><th>值</th><th>类型</th><th>范围</th><th style="width:72px"></th></tr></thead>
      <tbody>
        ${items.map((it) => `
          <tr>
            <td class="mono k">${esc(it.key)}</td>
            <td class="mono v">${it.is_secret
              ? `<span class="secret-masked">${esc(it.value)}</span>`
              : esc(it.value)}</td>
            <td><span class="meta-tag">${esc(cfgTypeLabel(it.value_type))}</span></td>
            <td><span class="meta-tag">${esc(cfgScopeLabel(it.scope))}</span></td>
            <td>${it.is_secret
              ? (canReveal
                ? `<button class="btn small" data-reveal="${it.id}" data-key="${esc(it.key)}">看明文</button>`
                : `<button class="btn small" disabled
                    title="${esc(revealDenyReason || "你没有密文查看权")}">看明文</button>`)
              : ""}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
  $$("[data-reveal]", box).forEach((btn) => {
    btn.onclick = () => openRevealModal(appIdFromHash(), btn.dataset.reveal, btn.dataset.key);
  });
}

function appIdFromHash() {
  return (location.hash.split("/")[2] || "").split("?")[0];
}

async function paintVersions(appId, environment, versions, readOnly, canEdit) {
  canEdit = canEdit !== false;
  const box = $("#cp-versions");
  if (!versions.length) {
    box.innerHTML = `<div class="empty-tip">还没有版本，首次保存后生成 v1</div>`;
    return;
  }
  box.innerHTML = versions.map((v) => `
    <div class="version-item ${v.is_current ? "current" : ""}">
      <div class="ver-head">
        <b>v${v.version}</b>${v.is_current ? '<span class="ver-cur">当前版本</span>' : ""}
        <span class="ver-meta">${v.item_count} 项 · ${esc(v.created_by_name)} · ${fmtAgo(v.created_at)}</span>
      </div>
      ${v.change_note ? `<div class="ver-note">${esc(v.change_note)}</div>` : ""}
      <div class="ver-actions">
        <button class="btn small" data-view="${v.version}">查看快照</button>
        ${(readOnly || !canEdit || v.is_current) ? "" : `<button class="btn small danger" data-rb="${v.version}">回滚到此版</button>`}
      </div>
    </div>`).join("");
  $$("[data-view]", box).forEach((b) => {
    b.onclick = () => openVersionSnapshot(appId, environment, +b.dataset.view);
  });
  $$("[data-rb]", box).forEach((b) => {
    b.onclick = () => openRollbackModal(appId, environment, +b.dataset.rb);
  });
}

/* ---------------- 密文查看（服务端强制理由） ---------------- */
function openRevealModal(appId, itemId, key) {
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:480px">
        <h3>查看密文明文 · <span class="mono">${esc(key)}</span></h3>
        <p style="color:var(--ink-2);font-size:13px">
          本次查看将由<b>服务端强制记录</b>：操作人、时间、键名与查看理由。请如实填写业务理由（不少于 5 个字）。
        </p>
        <div class="form-grid" style="grid-template-columns:1fr">
          <div>
            <label>查看理由 *</label>
            <textarea id="rv-reason" rows="3" placeholder="如：线上工单 INC-xxx 排查数据库连接问题"></textarea>
          </div>
        </div>
        <div class="form-error" id="rv-error"></div>
        <div class="form-actions">
          <button class="btn" id="rv-cancel">取消</button>
          <button class="btn primary" id="rv-submit">确认查看并留痕</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#rv-cancel").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  $("#rv-submit").onclick = async () => {
    const reason = $("#rv-reason").value.trim();
    const errBox = $("#rv-error");
    errBox.classList.remove("show");
    try {
      const res = await api(`/api/apps/${appId}/config/reveal`, {
        method: "POST", body: { item_id: +itemId, reason },
      });
      close();
      showRevealedValue(key, res.value);
    } catch (e) {
      errBox.textContent = e.message;
      errBox.classList.add("show");
    }
  };
}

function showRevealedValue(key, value) {
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:520px">
        <h3>明文（仅本次展示）· <span class="mono">${esc(key)}</span></h3>
        <div class="secret-plain mono">${esc(value)}</div>
        <p style="color:var(--red);font-size:12px">本次查看已记录到变更留痕，请勿截屏外发。</p>
        <div class="form-actions">
          <button class="btn primary" id="rv-done">我知道了</button>
        </div>
      </div>
    </div>`;
  $("#rv-done").onclick = () => { root.innerHTML = ""; };
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) root.innerHTML = ""; };
}

/* ---------------- 配置编辑 ---------------- */
function openConfigEditor(appId, environment, data, prefill) {
  const root = $("#modal-root");
  let rows;
  if (prefill && prefill.rows) {
    // 冲突合并后的预填：已按"对方改动保留、我的改动带上"处理
    rows = prefill.rows;
  } else {
    rows = data.items.map((it) => ({
      key: it.key,
      value: it.is_secret ? "" : it.value,   // 密文不回填明文
      value_type: it.value_type,
      scope: it.scope,
      is_secret: it.is_secret,
      keep_value: it.is_secret,              // 留空 = 不改密文
      existed: true,
    }));
  }
  if (!rows.length) rows.push(blankRow());
  const conflictKeys = prefill && prefill.conflictKeys ? prefill.conflictKeys : new Set();

  function blankRow() {
    return { key: "", value: "", value_type: "string", scope: "global", is_secret: false, keep_value: false, existed: false };
  }

  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:880px">
        <h3>编辑配置 · ${esc(data.app_name)} · ${esc(envLabel(environment))}环境
          <span class="ver-base">基线 v${data.current_version}</span></h3>
        ${conflictKeys.size ? `<div class="conflict-hint">以下键双方都改过，当前保留的是服务器最新值，请逐项确认（标红行）：${esc([...conflictKeys].join("、"))}</div>` : ""}
        <p style="color:var(--ink-3);font-size:12px;margin:0 0 10px">整体保存后生成一个新版本；密文值留空表示「不修改原密文」。保存会再次校验版本，防止并发覆盖。</p>
        <table class="env-table cfg-edit-table">
          <thead><tr>
            <th style="width:24%">键</th><th style="width:30%">值</th><th style="width:13%">类型</th>
            <th style="width:13%">范围</th><th style="width:9%">密文</th><th style="width:48px"></th>
          </tr></thead>
          <tbody id="ce-tbody"></tbody>
        </table>
        <button class="btn small" id="ce-add" style="margin-top:8px">+ 添加一行</button>
        <div class="form-grid" style="grid-template-columns:1fr;margin-top:12px">
          <div>
            <label>变更备注（可选）</label>
            <input id="ce-note" maxlength="200" placeholder="如：缩短支付超时 / 轮换数据库口令" value="${esc(prefill && prefill.note ? prefill.note : "")}">
          </div>
        </div>
        <div class="form-error" id="ce-error"></div>
        <div class="form-actions">
          <button class="btn" id="ce-cancel">取消</button>
          <button class="btn primary" id="ce-submit">保存为新版本</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#ce-cancel").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  $("#ce-add").onclick = () => { rows.push(blankRow()); paint(); };

  const tbody = $("#ce-tbody");
  function paint() {
    tbody.innerHTML = rows.map((r, i) => `
      <tr class="${conflictKeys.has(r.key) ? "row-conflict" : ""}">
        <td><input data-i="${i}" data-f="key" placeholder="如 DB_HOST" value="${esc(r.key)}"></td>
        <td>
          <input data-i="${i}" data-f="value" autocomplete="new-password"
            placeholder="${r.is_secret ? "密文已设置，留空不改" : "值"}" value="${esc(r.value)}">
        </td>
        <td>
          <select data-i="${i}" data-f="value_type">
            ${state.configMeta.types.map((t) => `<option value="${t.value}" ${r.value_type === t.value ? "selected" : ""}>${esc(t.label)}</option>`).join("")}
          </select>
        </td>
        <td>
          <select data-i="${i}" data-f="scope">
            ${state.configMeta.scopes.map((t) => `<option value="${t.value}" ${r.scope === t.value ? "selected" : ""}>${esc(t.label)}</option>`).join("")}
          </select>
        </td>
        <td style="text-align:center"><input type="checkbox" data-i="${i}" data-f="is_secret" ${r.is_secret ? "checked" : ""}></td>
        <td><button class="btn small danger" data-del="${i}">删</button></td>
      </tr>`).join("");
    $$("input,select", tbody).forEach((el) => {
      el.oninput = el.onchange = () => {
        const i = +el.dataset.i;
        if (el.dataset.f === "is_secret") {
          rows[i].is_secret = el.checked;
          if (el.checked && rows[i].existed) rows[i].keep_value = true;
          if (!el.checked) rows[i].keep_value = false;
          paint();
          return;
        }
        if (el.dataset.f === "value") {
          // 一旦手动输入新值，就不再沿用旧密文
          rows[i].keep_value = false;
        }
        rows[i][el.dataset.f] = el.value;
      };
    });
    $$("[data-del]", tbody).forEach((b) => {
      b.onclick = () => { rows.splice(+b.dataset.del, 1); if (!rows.length) rows.push(blankRow()); paint(); };
    });
  }
  paint();

  $("#ce-submit").onclick = async () => {
    const errBox = $("#ce-error");
    errBox.classList.remove("show");
    const items = rows.filter((r) => r.key.trim()).map((r) => ({
      key: r.key.trim(),
      value: r.value,
      value_type: r.value_type,
      scope: r.scope,
      is_secret: r.is_secret,
      keep_value: r.is_secret && r.existed && r.value === "",
    }));
    if (!items.length) {
      errBox.textContent = "至少保留一个配置键"; errBox.classList.add("show"); return;
    }
    const emptyNewSecret = items.find((it) => it.is_secret && !it.keep_value && it.value === "");
    if (emptyNewSecret) {
      errBox.textContent = `新增密文项 ${emptyNewSecret.key} 的值不能为空（已有密文可留空表示不修改）`;
      errBox.classList.add("show");
      return;
    }
    try {
      const res = await api(`/api/apps/${appId}/config?environment=${encodeURIComponent(environment)}`, {
        method: "PUT",
        // 乐观锁：带上打开编辑时的版本号，服务端据此识别并发改动
        body: { items, change_note: $("#ce-note").value.trim(), expected_version: data.current_version },
      });
      close();
      toast(`已生成 v${res.version}，${res.changes} 个键发生变化`, "success");
      paintConfigProfile(appId, environment);
    } catch (e) {
      if (e.status === 409 && e.payload && e.payload.code === "deploy_window_closed") {
        // 发布窗口外拦截：明确告知原因与下次开放时间，而不是只给一个灰按钮
        openWindowBlockedModal(e.payload);
        return;
      }
      if (e.status === 409 && e.payload) {
        // 并发冲突：弹出差异让用户取舍，而不是静默覆盖
        openConfigConflictModal(appId, environment, e.payload, { items, note: $("#ce-note").value.trim() }, close);
        return;
      }
      errBox.textContent = e.message;
      errBox.classList.add("show");
    }
  };
}

function openWindowBlockedModal(p) {
  const root = $("#modal-root");
  root.insertAdjacentHTML("beforeend", `
    <div class="modal-mask" id="wb-mask"><div class="modal" style="width:520px">
      <h3>🚫 当前不在发布窗口</h3>
      <p style="color:var(--ink-2);font-size:13px;line-height:1.7">${esc(p.message)}</p>
      <div class="window-bar closed" style="margin:8px 0 4px">
        <span>窗口规则：${esc(p.window_text)}</span>
      </div>
      ${p.next_open_label ? `<p style="font-size:15px;margin:12px 0">下一次能发布：<b style="color:#a02a2e">${esc(p.next_open_label)}</b></p>` : ""}
      <p style="color:var(--ink-3);font-size:12px;margin:0">本次保存已被服务端拒绝并记录到变更留痕；如需紧急发布，请联系有权限者调整窗口或解除当天封网。</p>
      <div class="form-actions"><button class="btn primary" id="wb-ok">我知道了</button></div>
    </div></div>`);
  $("#wb-ok").onclick = () => $("#wb-mask").remove();
  $("#wb-mask").onclick = (ev) => { if (ev.target.id === "wb-mask") $("#wb-mask").remove(); };
}

/* ---------------- 并发编辑冲突（乐观锁 409） ---------------- */
function openConfigConflictModal(appId, environment, p, draft, closeEditor) {
  const root = $("#modal-root");
  const interesting = (p.entries || []).filter((e) => e.status !== "same");
  const meta = p.current_version_meta || {};
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:860px">
        <h3>⚠️ 配置已被他人更新（v${p.base_version} → v${p.current_version}）</h3>
        <p style="color:var(--ink-2);font-size:13px;margin:4px 0 10px">${esc(p.message)}</p>
        <div class="conflict-meta">
          对方保存：<b>${esc(meta.created_by_name || "系统")}</b> · ${fmtTime(meta.created_at)}
          ${meta.change_note ? ` · 备注：${esc(meta.change_note)}` : ""}
        </div>
        <table class="env-table diff-table">
          <thead><tr><th>键</th><th>你打开时 v${p.base_version}</th><th></th><th>服务器当前 v${p.current_version}</th></tr></thead>
          <tbody>
            ${interesting.length ? interesting.map((e) => `
              <tr class="diff-${e.status}">
                <td class="mono">${esc(e.key)}</td>
                <td class="mono">${e.a ? sideValue(e.a) : '<span class="diff-gone">（无）</span>'}</td>
                <td>${diffArrow(e.status)}</td>
                <td class="mono">${e.b ? sideValue(e.b) : '<span class="diff-gone">（无）</span>'}</td>
              </tr>`).join("")
              : '<tr><td colspan="4" class="empty-tip">元数据变化（无值差异）</td></tr>'}
          </tbody>
        </table>
        <p style="color:var(--red);font-size:12px">系统不会让后来者静默覆盖前一个改动。请选择如何取舍：</p>
        <div class="form-actions">
          <button class="btn" id="cf-discard">放弃我的改动，刷新看最新版</button>
          <button class="btn primary" id="cf-merge">在最新版基础上重新合并（对方改动的键保留，待我确认）</button>
        </div>
      </div>
    </div>`;
  const closeModal = () => { root.innerHTML = ""; };
  $("#cf-discard").onclick = () => {
    closeModal(); closeEditor(); paintConfigProfile(appId, environment);
    toast("已放弃你的未保存改动，已刷新到最新版本", "");
  };
  $("#cf-merge").onclick = async () => {
    // 拉取最新版作为合并基底；对方改过的键不被我的草稿自动覆盖，标红待确认
    let latest;
    try {
      latest = await api(`/api/apps/${appId}/config?environment=${encodeURIComponent(environment)}`);
    } catch (e) { toast(e.message, "error"); return; }
    const otherChanged = new Set(interesting.map((e) => e.key));
    const draftMap = {};
    (draft.items || []).forEach((it) => { draftMap[it.key] = it; });
    const conflictKeySet = new Set();
    const merged = latest.items.map((it) => {
      const mine = draftMap[it.key];
      const row = {
        key: it.key,
        value: it.is_secret ? "" : it.value,
        value_type: it.value_type, scope: it.scope,
        is_secret: it.is_secret,
        keep_value: it.is_secret, existed: true,
      };
      if (mine && !otherChanged.has(it.key)) {
        // 对方没动这个键：安全地带上我的改动
        if (mine.keep_value) { row.value = ""; row.keep_value = true; }
        else { row.value = mine.value; row.keep_value = false; }
        row.value_type = mine.value_type; row.scope = mine.scope; row.is_secret = mine.is_secret;
      } else if (mine && otherChanged.has(it.key)) {
        // 双方都改了：保留服务器值，标红进入编辑器后人工确认
        row.conflict = true;
        conflictKeySet.add(it.key);
      }
      return row;
    });
    // 我新增的键（服务器当前没有）
    latest.items.forEach((it) => delete draftMap[it.key]);
    Object.values(draftMap).forEach((mine) => {
      merged.push({
        key: mine.key, value: mine.keep_value ? "" : mine.value,
        value_type: mine.value_type, scope: mine.scope, is_secret: mine.is_secret,
        keep_value: !!mine.keep_value, existed: false,
      });
    });
    closeModal(); closeEditor();
    openConfigEditor(appId, environment, latest, { rows: merged, note: draft.note, conflictKeys: conflictKeySet });
    toast(conflictKeySet.size
      ? "已载入最新版：标红的键双方都改过，请逐项确认后再保存"
      : "已载入最新版并带上你未冲突的改动，确认后保存", "");
  };
}

/* ---------------- 版本快照 ---------------- */
async function openVersionSnapshot(appId, environment, version) {
  let v;
  try {
    v = await api(`/api/apps/${appId}/config/versions/${version}?environment=${encodeURIComponent(environment)}`);
  } catch (e) { toast(e.message, "error"); return; }
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:720px">
        <h3>版本快照 v${v.version} ${v.is_current ? '<span class="ver-cur">当前版本</span>' : ""}</h3>
        ${v.change_note ? `<p style="color:var(--ink-2);font-size:13px;margin:0 0 8px">${esc(v.change_note)}</p>` : ""}
        <table class="env-table cfg-table">
          <thead><tr><th>键</th><th>值</th><th>类型</th><th>范围</th></tr></thead>
          <tbody>
            ${v.items.map((it) => `
              <tr>
                <td class="mono">${esc(it.key)}</td>
                <td class="mono">${it.is_secret ? `<span class="secret-masked">${esc(it.value)}</span>` : esc(it.value)}</td>
                <td>${esc(cfgTypeLabel(it.value_type))}</td>
                <td>${esc(cfgScopeLabel(it.scope))}</td>
              </tr>`).join("")}
          </tbody>
        </table>
        <div class="form-actions"><button class="btn primary" id="vs-done">关闭</button></div>
      </div>
    </div>`;
  $("#vs-done").onclick = () => { root.innerHTML = ""; };
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) root.innerHTML = ""; };
}

/* ---------------- 回滚（追加新版本，不覆盖留痕） ---------------- */
async function openRollbackModal(appId, environment, targetVersion) {
  let preview;
  try {
    preview = await api(`/api/apps/${appId}/config/rollback-preview?environment=${encodeURIComponent(environment)}&version=${targetVersion}`);
  } catch (e) { toast(e.message, "error"); return; }
  const root = $("#modal-root");
  const diffRows = preview.entries.filter((e) => e.status !== "same");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:820px">
        <h3>回滚：v${preview.from_version} → v${targetVersion}</h3>
        <p style="color:var(--ink-2);font-size:13px;margin:0 0 8px">
          回滚会<b>追加一个新版本</b>（内容同 v${targetVersion}），v${preview.from_version} 及其全部留痕<b>原样保留、不会被覆盖</b>。
        </p>
        ${diffRows.length ? `
        <table class="env-table diff-table">
          <thead><tr><th>键</th><th>当前 v${preview.from_version}</th><th></th><th>回滚后 v${targetVersion}</th></tr></thead>
          <tbody>
            ${diffRows.map((e) => `
              <tr class="diff-${e.status}">
                <td class="mono">${esc(e.key)}</td>
                <td class="mono">${e.a ? sideValue(e.a) : '<span class="diff-gone">（不存在）</span>'}</td>
                <td>${diffArrow(e.status)}</td>
                <td class="mono">${e.b ? sideValue(e.b) : '<span class="diff-gone">（将删除）</span>'}</td>
              </tr>`).join("")}
          </tbody>
        </table>` : `<p>两个版本内容一致，无需回滚。</p>`}
        <div class="form-error" id="rb-error"></div>
        <div class="form-actions">
          <button class="btn" id="rb-cancel">取消</button>
          <button class="btn danger" id="rb-submit" ${diffRows.length ? "" : "disabled"}>确认回滚（生成新版本）</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#rb-cancel").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  $("#rb-submit").onclick = async () => {
    try {
      const res = await api(`/api/apps/${appId}/config/rollback`, {
        method: "POST",
        body: { environment, version: targetVersion, expected_version: preview.from_version },
      });
      close();
      toast(`已回滚：生成 v${res.version}，${res.changes} 个键变化；历史留痕已保留`, "success");
      paintConfigProfile(appId, environment);
    } catch (e) {
      if (e.status === 409 && e.payload && e.payload.code === "deploy_window_closed") {
        close();
        openWindowBlockedModal(e.payload);
        return;
      }
      if (e.status === 409 && e.payload) {
        // 回滚前又有人保存了新版本：同样不能静默覆盖，弹冲突提示并刷新
        close();
        openConfigConflictModal(appId, environment, e.payload, { items: [], note: "" }, () => {});
        return;
      }
      const box = $("#rb-error");
      box.textContent = e.message; box.classList.add("show");
    }
  };
}

function sideValue(side) {
  return side.is_secret ? `<span class="secret-masked">${esc(side.value)}</span>` : esc(side.value);
}
function diffArrow(status) {
  return { only_a: "→ 删除", only_b: "← 恢复", changed: "→" }[status] || "";
}

/* ---------------- 环境对比 ---------------- */
function openEnvDiffModal(appId, curEnv, visibleEnvObjs) {
  const root = $("#modal-root");
  // visibleEnvObjs：该应用已注册且当前账号可见的环境 {environment, environment_label}
  const all = visibleEnvObjs && visibleEnvObjs.length ? visibleEnvObjs : [];
  const visibleEnvs = all;
  const otherEnvs = visibleEnvs.filter((e) => e.environment !== curEnv);
  const curName = (all.find((e) => e.environment === curEnv) || {}).environment_label || envLabel(curEnv);
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:860px">
        <h3>环境配置差异对比</h3>
        ${visibleEnvs.length < 2 ? `<p style="color:var(--red);font-size:13px">你只有一个环境（${esc(curName)}）的可见权，无法做环境对比；其他环境需另行授权。</p>` : ""}
        <div class="filter-bar" style="padding:0 0 12px;box-shadow:none;border:0">
          <span style="font-size:13px;color:var(--ink-2)">基准环境</span>
          <select id="df-a">
            ${visibleEnvs.map((e) => `<option value="${e.environment}" ${e.environment === curEnv ? "selected" : ""}>${esc(e.environment_label)}</option>`).join("")}
          </select>
          <span style="font-size:13px;color:var(--ink-2)">对比环境</span>
          <select id="df-b">
            ${otherEnvs.map((e) => `<option value="${e.environment}" ${e.environment === "prod" && curEnv !== "prod" ? "selected" : ""}>${esc(e.environment_label)}</option>`).join("")}
          </select>
          <button class="btn primary small" id="df-run" ${otherEnvs.length ? "" : "disabled"}>对比</button>
        </div>
        <div id="df-result"><div class="empty-tip">${otherEnvs.length ? "选择两个环境后点「对比」" : "没有第二个可见环境可对比"}</div></div>
        <div class="form-actions"><button class="btn" id="df-done">关闭</button></div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#df-done").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  const run = async () => {
    const a = $("#df-a").value, b = $("#df-b").value;
    if (a === b) { $("#df-result").innerHTML = `<div class="empty-tip">请选择两个不同的环境</div>`; return; }
    $("#df-result").innerHTML = `<div class="empty-tip">对比中…</div>`;
    try {
      const d = await api(`/api/apps/${appId}/config/diff?env_a=${a}&env_b=${b}`);
      paintDiffResult(d);
    } catch (e) { $("#df-result").innerHTML = `<div class="empty-tip">${esc(e.message)}</div>`; }
  };
  $("#df-run").onclick = run;
  run();
}

function paintDiffResult(d) {
  const s = d.summary;
  const interesting = d.entries.filter((e) => e.status !== "same");
  const box = $("#df-result");
  box.innerHTML = `
    <div class="diff-summary">
      <span class="ds changed">值不同 ${s.changed}</span>
      <span class="ds onlya">仅${envLabel(d.env_a)}有 ${s.only_a}</span>
      <span class="ds onlyb">仅${envLabel(d.env_b)}有 ${s.only_b}</span>
      <span class="ds same">一致 ${s.same}</span>
    </div>
    ${interesting.length ? `
    <table class="env-table diff-table">
      <thead><tr><th>键</th><th>${esc(envLabel(d.env_a))}</th><th></th><th>${esc(envLabel(d.env_b))}</th></tr></thead>
      <tbody>
        ${interesting.map((e) => `
          <tr class="diff-${e.status}">
            <td class="mono">${esc(e.key)}</td>
            <td class="mono">${e.a ? sideValue(e.a) : '<span class="diff-gone">（无）</span>'}</td>
            <td>${diffArrow(e.status)}</td>
            <td class="mono">${e.b ? sideValue(e.b) : '<span class="diff-gone">（无）</span>'}</td>
          </tr>`).join("")}
      </tbody>
    </table>` : `<div class="empty-tip">两个环境配置完全一致 🎉</div>`}`;
}

/* ---------------- 变更留痕 ---------------- */
let auditTab = "config";

async function renderAudit() {
  const view = $("#view");
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>变更留痕</h2>
        <div class="sub">配置逐键流水，以及发布窗口调整、节假日封网、实例掉线/重启等环境与健康变更</div>
      </div>
    </div>
    <div class="admin-tabs">
      <button class="adm-tab ${auditTab === "config" ? "active" : ""}" data-at="config">配置留痕</button>
      <button class="adm-tab ${auditTab === "ops" ? "active" : ""}" data-at="ops">环境与健康留痕</button>
    </div>
    <div id="audit-body"><div class="empty-tip">加载中…</div></div>`;
  $$(".adm-tab", view).forEach((b) => {
    b.onclick = () => { auditTab = b.dataset.at; renderAudit(); };
  });
  if (auditTab === "config") renderConfigAudit();
  else renderOpsAudit();
}

async function renderConfigAudit() {
  const f = state.auditFilters;
  const isAdmin = state.user.role === "admin";
  $("#audit-body").innerHTML = `
    <div class="panel filter-bar">
      <select id="au-bl" ${isAdmin ? "" : "disabled"}>
        <option value="">全部业务线</option>
        ${state.businessLines.map((b) => `<option value="${b.id}" ${String(b.id) === String(f.business_line_id) ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
      </select>
      <input id="au-app" placeholder="应用 ID（可留空）" value="${esc(f.app_id)}" style="width:130px">
      <select id="au-env">
        <option value="">全部环境</option>
        ${state.envCatalog.map((e) => `<option value="${e.value}" ${f.environment === e.value ? "selected" : ""}>${esc(e.label)}</option>`).join("")}
      </select>
      <select id="au-action">
        <option value="">全部动作</option>
        ${state.configMeta.actions.map((a) => `<option value="${a.value}" ${f.action === a.value ? "selected" : ""}>${esc(a.label)}</option>`).join("")}
      </select>
      <input type="date" id="au-start" value="${esc(f.start)}" title="开始日期">
      <span style="color:var(--ink-3)">至</span>
      <input type="date" id="au-end" value="${esc(f.end)}" title="结束日期">
      <button class="btn primary" id="au-run">查询</button>
      <button class="btn" id="au-reset">重置</button>
      <button class="btn primary" id="au-export" style="margin-left:auto">导出差异清单 CSV</button>
    </div>
    <div id="au-result"><div class="empty-tip">加载中…</div></div>`;

  const buildParams = () => {
    const p = new URLSearchParams();
    if (isAdmin && $("#au-bl").value) p.set("business_line_id", $("#au-bl").value);
    if ($("#au-app").value.trim()) p.set("app_id", $("#au-app").value.trim());
    if ($("#au-env").value) p.set("environment", $("#au-env").value);
    if ($("#au-action").value) p.set("action", $("#au-action").value);
    if ($("#au-start").value) p.set("start", $("#au-start").value);
    if ($("#au-end").value) p.set("end", $("#au-end").value);
    return p;
  };
  const load = async () => {
    try {
      const rows = await api(`/api/config/audit?${buildParams().toString()}`);
      paintAuditRows(rows);
    } catch (e) {
      $("#au-result").innerHTML = errorStateHtml("查询失败", e.message);
    }
  };
  $("#au-run").onclick = () => {
    state.auditFilters = {
      app_id: $("#au-app").value.trim(), business_line_id: $("#au-bl").value,
      environment: $("#au-env").value, action: $("#au-action").value,
      start: $("#au-start").value, end: $("#au-end").value,
    };
    load();
  };
  $("#au-reset").onclick = () => {
    state.auditFilters = { app_id: "", business_line_id: "", environment: "", action: "", start: "", end: "" };
    renderAudit();
  };
  $("#au-export").onclick = () => {
    // 带当前筛选条件与令牌下载（令牌在 header 里，用 fetch 转 blob）
    fetch(`/api/config/audit/export.csv?${buildParams().toString()}`, {
      headers: { "X-Token": state.token },
    }).then(async (res) => {
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `导出失败（HTTP ${res.status}）`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `config-changelog-${Date.now()}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    }).catch((e) => toast(e.message, "error"));
  };
  await load();
}

async function renderOpsAudit() {
  const f = state.opsFilters;
  const isAdmin = state.user.role === "admin";
  let cats = [];
  try {
    cats = (await api("/api/ops/audit/meta")).categories;
  } catch (e) { /* 忽略，用兜底 */ }
  $("#audit-body").innerHTML = `
    <div class="panel filter-bar">
      <select id="op-bl" ${isAdmin ? "" : "disabled"}>
        <option value="">全部业务线</option>
        ${state.businessLines.map((b) => `<option value="${b.id}" ${String(b.id) === String(f.business_line_id) ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
      </select>
      <input id="op-app" placeholder="应用 ID（可留空）" value="${esc(f.app_id)}" style="width:130px">
      <select id="op-env">
        <option value="">全部环境</option>
        ${state.envCatalog.map((e) => `<option value="${e.value}" ${f.environment === e.value ? "selected" : ""}>${esc(e.label)}</option>`).join("")}
      </select>
      <select id="op-cat">
        <option value="">全部类别</option>
        ${cats.map((c) => `<option value="${c.value}" ${f.category === c.value ? "selected" : ""}>${esc(c.label)}</option>`).join("")}
      </select>
      <input type="date" id="op-start" value="${esc(f.start)}" title="开始日期">
      <span style="color:var(--ink-3)">至</span>
      <input type="date" id="op-end" value="${esc(f.end)}" title="结束日期">
      <button class="btn primary" id="op-run">查询</button>
      <button class="btn" id="op-reset">重置</button>
      <button class="btn primary" id="op-export" style="margin-left:auto">导出 CSV</button>
    </div>
    <div id="op-result"><div class="empty-tip">加载中…</div></div>`;

  const buildParams = () => {
    const p = new URLSearchParams();
    if (isAdmin && $("#op-bl").value) p.set("business_line_id", $("#op-bl").value);
    if ($("#op-app").value.trim()) p.set("app_id", $("#op-app").value.trim());
    if ($("#op-env").value) p.set("environment", $("#op-env").value);
    if ($("#op-cat").value) p.set("category", $("#op-cat").value);
    if ($("#op-start").value) p.set("start", $("#op-start").value);
    if ($("#op-end").value) p.set("end", $("#op-end").value);
    return p;
  };
  const load = async () => {
    try {
      const rows = await api(`/api/ops/audit?${buildParams().toString()}`);
      paintOpsRows(rows);
    } catch (e) {
      $("#op-result").innerHTML = errorStateHtml("查询失败", e.message);
    }
  };
  $("#op-run").onclick = () => {
    state.opsFilters = {
      app_id: $("#op-app").value.trim(), business_line_id: $("#op-bl").value,
      environment: $("#op-env").value, category: $("#op-cat").value,
      start: $("#op-start").value, end: $("#op-end").value,
    };
    load();
  };
  $("#op-reset").onclick = () => {
    state.opsFilters = { app_id: "", business_line_id: "", environment: "", category: "", start: "", end: "" };
    renderAudit();
  };
  $("#op-export").onclick = () => {
    fetch(`/api/ops/audit/export.csv?${buildParams().toString()}`, {
      headers: { "X-Token": state.token },
    }).then(async (res) => {
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `导出失败（HTTP ${res.status}）`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `ops-changelog-${Date.now()}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    }).catch((e) => toast(e.message, "error"));
  };
  await load();
}

function paintOpsRows(rows) {
  const box = $("#op-result");
  if (!rows.length) {
    box.innerHTML = `<div class="panel panel-pad empty-tip">该时间窗内没有环境与健康变更记录</div>`;
    return;
  }
  box.innerHTML = `
    <div class="panel table-wrap">
      <table class="app-table audit-table">
        <thead><tr>
          <th>时间</th><th>应用</th><th>环境</th><th>类别</th><th>对象</th><th>详情</th><th>操作人</th>
        </tr></thead>
        <tbody>
          ${rows.map((r) => `
            <tr>
              <td class="nowrap">${fmtTime(r.created_at)}</td>
              <td class="app-name-cell">${esc(r.app_name)}<div class="app-desc">${esc(r.business_line_name)}</div></td>
              <td><span class="env-tag ${r.environment === "prod" ? "prod" : ""}">${esc(r.environment_label)}</span></td>
              <td><span class="action-tag ${esc(r.category)}">${esc(r.category_label)}</span></td>
              <td class="mono">${esc(r.target_name || "—")}</td>
              <td class="reason-cell">${esc(r.detail)}</td>
              <td class="nowrap">${esc(r.user_name)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>
    <p style="color:var(--ink-3);font-size:12px;margin:8px 2px">
      窗口规则改动、节假日封网、实例掉线/恢复/重启、窗口外拦截上线都在此留痕，最多返回最近 500 条。
    </p>`;
}

function paintAuditRows(rows) {
  const box = $("#au-result");
  if (!rows.length) {
    box.innerHTML = `<div class="panel panel-pad empty-tip">该时间窗内没有配置变更流水</div>`;
    return;
  }
  box.innerHTML = `
    <div class="panel table-wrap">
      <table class="app-table audit-table">
        <thead><tr>
          <th>时间</th><th>应用</th><th>环境</th><th>动作</th><th>配置键</th>
          <th>改前</th><th>改后</th><th>操作人</th><th>理由/备注</th>
        </tr></thead>
        <tbody>
          ${rows.map((r) => `
            <tr>
              <td class="nowrap">${fmtTime(r.created_at)}</td>
              <td class="app-name-cell">${esc(r.app_name)}<div class="app-desc">${esc(r.business_line_name)}</div></td>
              <td><span class="env-tag ${esc(r.environment)}">${esc(r.environment_label === r.environment ? envLabel(r.environment) : r.environment_label)}</span></td>
              <td><span class="action-tag ${esc(r.action)}">${esc(r.action_label)}</span>${r.is_secret ? ' <span title="涉及密文">🔒</span>' : ""}</td>
              <td class="mono">${esc(r.config_key || "—")}</td>
              <td class="mono audit-val">${r.old_value === null ? '<span class="diff-gone">—</span>' : esc(r.old_value)}</td>
              <td class="mono audit-val">${r.new_value === null ? '<span class="diff-gone">—</span>' : esc(r.new_value)}</td>
              <td class="nowrap">${esc(r.user_name)}</td>
              <td class="reason-cell">${esc(r.reason || "")}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>
    <p style="color:var(--ink-3);font-size:12px;margin:8px 2px">最多返回最近 500 条；如需完整流水请用「导出差异清单 CSV」。</p>`;
}

/* ---------------- 权限与交接 ---------------- */
async function openTransferModal(app) {
  let users;
  try {
    users = await api("/api/users");
  } catch (e) { toast(e.message, "error"); return; }
  const candidates = users.filter((u) => u.role !== "admin" && u.role !== "viewer"
    && String(u.business_line_id) === String(app.business_line_id)
    && u.id !== app.owner_id);
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:560px">
        <h3>应用交接 · ${esc(app.name)}</h3>
        <p style="color:var(--ink-2);font-size:13px">
          当前负责人：<b>${esc(app.owner_name || "（空缺）")}</b>。交接后应用归属与配置管理权限移交新负责人，
          配置项、历史版本、密文与全部留痕随应用一并移交，本次交接会留痕。
        </p>
        <div class="form-grid" style="grid-template-columns:1fr">
          <div>
            <label>交接给（须属于 ${esc(app.business_line_name)}）*</label>
            <select id="tf-owner">
              <option value="">选择新负责人…</option>
              ${candidates.map((u) => `<option value="${u.id}">${esc(u.name)}（${esc(roleLabelOf(u.role))}）</option>`).join("")}
            </select>
          </div>
          <div>
            <label>交接原因 / 备注</label>
            <textarea id="tf-note" rows="3" placeholder="如：离职 / 转岗 / 团队调整，说明交接前后责任"></textarea>
          </div>
        </div>
        <div class="form-error" id="tf-error"></div>
        <div class="form-actions">
          <button class="btn" id="tf-cancel">取消</button>
          <button class="btn primary" id="tf-submit">确认交接并留痕</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#tf-cancel").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  $("#tf-submit").onclick = async () => {
    const box = $("#tf-error");
    box.classList.remove("show");
    const newOwner = +$("#tf-owner").value;
    if (!newOwner) { box.textContent = "请选择交接后的新负责人"; box.classList.add("show"); return; }
    try {
      const r = await api(`/api/apps/${app.id}/transfer`, {
        method: "POST", body: { new_owner_id: newOwner, note: $("#tf-note").value.trim() },
      });
      close();
      toast(r.detail || "交接完成并留痕", "success");
      renderAppDetail(app.id);
    } catch (e) { box.textContent = e.message; box.classList.add("show"); }
  };
}

async function renderAdmin() {
  const view = $("#view");
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>权限与交接</h2>
        <div class="sub">按 业务线 × 环境 收口可见范围；密文查看权与配置编辑权分开授予；所有权限变更与交接均留痕</div>
      </div>
    </div>
    <div class="admin-tabs">
      <button class="adm-tab active" data-tab="people">人员与授权</button>
      <button class="adm-tab" data-tab="transfer">应用交接</button>
      <button class="adm-tab" data-tab="logs">权限变更留痕</button>
    </div>
    <div id="admin-body"><div class="empty-tip">加载中…</div></div>`;
  $$(".adm-tab", view).forEach((b) => {
    b.onclick = () => {
      $$(".adm-tab", view).forEach((x) => x.classList.toggle("active", x === b));
      if (b.dataset.tab === "people") renderAdminPeople();
      else if (b.dataset.tab === "transfer") renderAdminTransfer();
      else renderAdminLogs();
    };
  });
  renderAdminPeople();
}

function roleLabelOf(role) {
  const r = (state.meta.roles || []).find((x) => x.value === role);
  return r ? r.label : role;
}

async function renderAdminPeople() {
  const body = $("#admin-body");
  let users;
  try {
    users = await api("/api/users");
  } catch (e) { body.innerHTML = errorStateHtml("加载失败", e.message); return; }
  const me = state.user;
  // 谁能打开谁的权限面板：管理员任意；业务线负责人本业务线；其他人只能看自己
  const canManageUser = (u) => {
    if (me.role === "admin") return true;
    if (me.role === "bl_owner") return u.business_line_id === me.business_line_id;
    return u.id === me.id;
  };
  const canFetchAny = me.role === "admin" || me.role === "bl_owner";
  body.innerHTML = `
    <div class="panel table-wrap">
      <table class="app-table">
        <thead><tr><th>账号</th><th>角色</th><th>所属业务线</th>${canFetchAny ? "<th>负责应用</th>" : ""}<th style="width:120px"></th></tr></thead>
        <tbody>
          ${users.map((u) => `
            <tr data-uid="${u.id}">
              <td><b>${esc(u.name)}</b><div class="app-desc mono">${esc(u.username)}</div></td>
              <td><span class="u-role role-${esc(u.role)}">${esc(roleLabelOf(u.role))}</span></td>
              <td>${esc(u.business_line_name || "—")}</td>
              ${canFetchAny ? `<td class="owned-cell" data-uid="${u.id}">…</td>` : ""}
              <td>${canManageUser(u)
                ? `<button class="btn small" data-open="${u.id}">${u.id === me.id ? "查看我的权限" : "查看/调整权限"}</button>`
                : '<span style="color:var(--ink-3);font-size:12px">无权管理</span>'}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>
    <div id="perm-detail" style="margin-top:16px"></div>`;
  $$("[data-open]", body).forEach((btn) => {
    btn.onclick = () => openPermDetail(+btn.dataset.open);
  });
  // 管理员/业务线负责人补每个用户负责应用；其他角色不批量请求（无权看他人明细）
  if (canFetchAny) {
    users.forEach(async (u) => {
      try {
        const d = await api(`/api/admin/users/${u.id}/permissions`);
        const cell = body.querySelector(`.owned-cell[data-uid="${u.id}"]`);
        if (cell) cell.innerHTML = d.owned_apps.length
          ? d.owned_apps.map((a) => esc(a.name)).join("、") : '<span style="color:var(--ink-3)">无</span>';
      } catch (e) { /* 忽略 */ }
    });
  }
}

async function openPermDetail(uid) {
  const box = $("#perm-detail");
  box.innerHTML = `<div class="empty-tip">加载权限明细…</div>`;
  let d;
  try {
    d = await api(`/api/admin/users/${uid}/permissions`);
  } catch (e) { box.innerHTML = errorStateHtml(e.status === 403 ? "403 无权查看" : "加载失败", e.message); return; }
  const u = d.user;
  const isAdmin = state.user.role === "admin";
  box.innerHTML = `
    <div class="panel panel-pad">
      <div class="cfg-toolbar">
        <h3 style="margin:0">${esc(u.name)}（${esc(roleLabelOf(u.role))}）的权限</h3>
        ${d.can_assign_role ? `
        <div style="display:flex;gap:8px;align-items:center">
          <select id="pm-role">
            ${state.meta.roles.map((r) => `<option value="${r.value}" ${r.value === u.role ? "selected" : ""}>${esc(r.label)}</option>`).join("")}
          </select>
          <button class="btn small" id="pm-role-save">保存角色</button>
        </div>` : ""}
      </div>

      <h4 style="margin:16px 0 8px">已授权范围（业务线 × 环境）</h4>
      <table class="env-table">
        <thead><tr><th>业务线</th><th>环境</th><th>配置查看</th><th>配置编辑</th><th>密文查看明文</th><th>授权人</th><th></th></tr></thead>
        <tbody>
          ${d.grants.length ? d.grants.map((g) => `
            <tr>
              <td>${esc(g.business_line_name)}</td>
              <td>${esc(g.environment_label)}</td>
              <td>${g.can_view_config ? "✅" : "—"}</td>
              <td>${g.can_edit_config ? "✅" : "—"}</td>
              <td>${g.can_reveal ? "✅" : "—"}</td>
              <td>${esc(g.granted_by_name || "系统")}</td>
              <td>${d.can_manage ? `<button class="btn small danger" data-revoke="${g.id}">收回</button>` : ""}</td>
            </tr>`).join("")
            : '<tr><td colspan="7" class="empty-tip">暂无任何环境授权（应用负责人对本人负责的应用默认可见/可编辑，密文仍需单独授权）</td></tr>'}
        </tbody>
      </table>

      ${d.can_manage ? `
      <h4 style="margin:18px 0 8px">新增 / 调整授权</h4>
      <div class="filter-bar" style="box-shadow:none;border:0;padding:0">
        <select id="pm-bl">
          ${state.businessLines.map((b) => `<option value="${b.id}">${esc(b.name)}</option>`).join("")}
        </select>
        <select id="pm-env">
          <option value="*">全部环境</option>
          ${state.envCatalog.map((e) => `<option value="${e.value}">${esc(e.label)}${e.is_builtin ? "" : "（自定义）"}</option>`).join("")}
        </select>
        <label class="chk"><input type="checkbox" id="pm-edit"> 配置编辑权</label>
        <label class="chk"><input type="checkbox" id="pm-reveal"> 密文查看权（与编辑独立）</label>
        <button class="btn primary small" id="pm-grant">授予 / 更新</button>
      </div>
      <p style="color:var(--ink-3);font-size:12px;margin:6px 0 0">
        查看（脱敏）随授权默认开放；只读观察者角色即使勾选编辑也会被服务端拒绝；密文查看权始终独立。
      </p>` : `<p style="color:var(--ink-3);font-size:12px;margin:12px 0 0">你只能查看该账号权限，调整授权请联系平台管理员或对应业务线负责人。</p>`}
      <div class="form-error" id="pm-error"></div>
    </div>`;

  const errBox = $("#pm-error");
  const showErr = (msg) => { errBox.textContent = msg; errBox.classList.add("show"); };
  if (d.can_assign_role) {
    $("#pm-role-save").onclick = async () => {
      errBox.classList.remove("show");
      try {
        await api(`/api/admin/users/${uid}/role`, {
          method: "PUT", body: { role: $("#pm-role").value },
        });
        toast("角色已调整并留痕", "success");
        renderAdminPeople(); openPermDetail(uid);
      } catch (e) { showErr(e.message); }
    };
  }
  if (d.can_manage) {
    $("#pm-grant").onclick = async () => {
      errBox.classList.remove("show");
      try {
        const r = await api(`/api/admin/users/${uid}/grants`, {
          method: "PUT",
          body: {
            business_line_id: +$("#pm-bl").value,
            environment: $("#pm-env").value,
            can_edit_config: $("#pm-edit").checked,
            can_reveal: $("#pm-reveal").checked,
          },
        });
        toast(r.detail || "授权已更新并留痕", "success");
        openPermDetail(uid);
      } catch (e) { showErr(e.message); }
    };
  }
  $$("[data-revoke]", box).forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm("确认收回该业务线×环境的全部授权？本次收回会写入留痕。")) return;
      try {
        const r = await api(`/api/admin/users/${uid}/grants/${btn.dataset.revoke}`, { method: "DELETE" });
        toast(r.detail || "已收回并留痕", "success");
        openPermDetail(uid);
      } catch (e) { showErr(e.message); }
    };
  });
}

async function renderAdminTransfer() {
  const body = $("#admin-body");
  let apps, users, transfers;
  try {
    [apps, users, transfers] = await Promise.all([
      api("/api/apps"), api("/api/users"), api("/api/admin/transfers"),
    ]);
  } catch (e) { body.innerHTML = errorStateHtml("加载失败", e.message); return; }
  const appById = {};
  apps.forEach((a) => { appById[a.id] = a; });
  body.innerHTML = `
    <div class="panel panel-pad" style="margin-bottom:16px">
      <h3>发起应用交接</h3>
      <p style="color:var(--ink-2);font-size:13px">交接的是应用归属与配置管理权限：配置项、历史版本、密文与全部留痕随应用一并移交；交接前后负责人、发起人与时间会写清楚。</p>
      <div class="filter-bar" style="box-shadow:none;border:0;padding:0">
        <select id="tr-app">
          <option value="">选择要交接的应用…</option>
          ${apps.map((a) => `<option value="${a.id}">${esc(a.name)}（${esc(a.business_line_name)} · 当前负责人：${esc(a.owner_name || "空缺")}）</option>`).join("")}
        </select>
        <select id="tr-owner"><option value="">先选择应用</option></select>
        <input id="tr-note" class="grow" placeholder="交接原因 / 备注（如：离职、转岗、团队调整）">
        <button class="btn primary" id="tr-submit">确认交接并留痕</button>
      </div>
      <div class="form-error" id="tr-error"></div>
    </div>
    <div class="panel table-wrap">
      <h3 style="padding:14px 16px 0">交接记录</h3>
      <table class="app-table">
        <thead><tr><th>时间</th><th>应用</th><th>业务线</th><th>交接前负责人</th><th></th><th>交接后负责人</th><th>发起人</th><th>备注</th></tr></thead>
        <tbody>
          ${transfers.length ? transfers.map((t) => `
            <tr>
              <td class="nowrap">${fmtTime(t.created_at)}</td>
              <td>${esc(t.app_name)}</td>
              <td>${esc(t.business_line_name)}</td>
              <td>${esc(t.old_owner_name || "（空缺）")}</td>
              <td>→</td>
              <td><b>${esc(t.new_owner_name)}</b></td>
              <td>${esc(t.transfer_by_name || "系统")}</td>
              <td class="reason-cell">${esc(t.note || "")}</td>
            </tr>`).join("")
            : '<tr><td colspan="8" class="empty-tip">暂无交接记录</td></tr>'}
        </tbody>
      </table>
    </div>`;

  const errBox = $("#tr-error");
  $("#tr-app").onchange = () => {
    const a = appById[+$("#tr-app").value];
    const sel = $("#tr-owner");
    if (!a) { sel.innerHTML = "<option>先选择应用</option>"; return; }
    const candidates = users.filter((u) => u.role !== "admin" && u.role !== "viewer"
      && String(u.business_line_id) === String(a.business_line_id));
    sel.innerHTML = `<option value="">选择新负责人（须属于${esc(a.business_line_name)}）…</option>`
      + candidates.map((u) => `<option value="${u.id}">${esc(u.name)}（${esc(roleLabelOf(u.role))}）</option>`).join("");
  };
  $("#tr-submit").onclick = async () => {
    errBox.classList.remove("show");
    const appId = +$("#tr-app").value;
    const newOwner = +$("#tr-owner").value;
    if (!appId) { errBox.textContent = "请选择要交接的应用"; errBox.classList.add("show"); return; }
    if (!newOwner) { errBox.textContent = "请选择交接后的新负责人"; errBox.classList.add("show"); return; }
    try {
      const r = await api(`/api/apps/${appId}/transfer`, {
        method: "POST", body: { new_owner_id: newOwner, note: $("#tr-note").value.trim() },
      });
      toast(r.detail || "交接完成并留痕", "success");
      renderAdminTransfer();
    } catch (e) { errBox.textContent = e.message; errBox.classList.add("show"); }
  };
}

async function renderAdminLogs() {
  const body = $("#admin-body");
  let logs, transfers;
  try {
    [logs, transfers] = await Promise.all([
      api("/api/admin/permission-logs"), api("/api/admin/transfers"),
    ]);
  } catch (e) { body.innerHTML = errorStateHtml("加载失败", e.message); return; }
  body.innerHTML = `
    <div class="panel table-wrap">
      <h3 style="padding:14px 16px 0">权限变更留痕（授权 / 收权 / 角色调整 / 交接）</h3>
      <table class="app-table audit-table">
        <thead><tr><th>时间</th><th>操作人</th><th>对象</th><th>动作</th><th>范围</th><th>详情</th></tr></thead>
        <tbody>
          ${logs.length ? logs.map((l) => `
            <tr>
              <td class="nowrap">${fmtTime(l.created_at)}</td>
              <td>${esc(l.actor_name)}</td>
              <td>${esc(l.target_name)}</td>
              <td><span class="action-tag ${esc(l.action)}">${esc(l.action_label)}</span></td>
              <td>${esc(l.scope_text || "—")}</td>
              <td class="reason-cell">${esc(l.detail)}</td>
            </tr>`).join("")
            : '<tr><td colspan="6" class="empty-tip">暂无权限变更记录</td></tr>'}
        </tbody>
      </table>
    </div>
    <p style="color:var(--ink-3);font-size:12px;margin:8px 2px">应用交接同时写入该应用的「变更记录」；配置逐键改动见「变更留痕」页。</p>`;
}

/* ---------------- 环境管理 & 发布窗口 ---------------- */
const WEEK_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

async function openEnvManagerModal(app) {
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask"><div class="modal" style="width:760px">
      <h3>环境与发布窗口 · ${esc(app.name || "")}</h3>
      <p style="color:var(--ink-2);font-size:12.5px;margin:0 0 12px">
        开发/预发/生产不是写死的：可在本应用下新增自定义环境；每个环境单独设发布窗口（周几 × 时段）与节假日封网；
        删除前会清点配置、版本与实例，有挂载的环境不允许删除。
      </p>
      <div id="em-list"><div class="empty-tip">加载环境…</div></div>
      <div id="em-add" style="margin-top:14px"></div>
      <div class="form-actions"><button class="btn primary" id="em-done">完成</button></div>
    </div></div>`;
  const close = () => { root.innerHTML = ""; };
  $("#em-done").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  await paintEnvList(app);
}

async function paintEnvList(app) {
  const box = $("#em-list");
  let envs;
  try {
    envs = await api(`/api/apps/${app.id}/environments`);
  } catch (e) { box.innerHTML = `<div class="empty-tip">${esc(e.message)}</div>`; return; }
  const canManage = envs.some((e) => e.can_manage);
  box.innerHTML = `
    <div class="env-grid">
      ${envs.map((e) => {
        const h = e.health || {};
        return `
        <div class="panel env-card ${h.level === "offline" ? "has-offline" : ""}">
          <div class="ec-head">
            <span class="ec-name">${esc(e.env_label)}</span>
            ${e.is_primary ? '<span class="ec-primary">应用所属环境</span>' : ""}
            ${e.is_builtin ? "" : '<span class="ec-tag">自定义</span>'}
          </div>
          <div class="ec-window" title="${esc(e.window_text)}">🕒 ${esc(e.window_text)}</div>
          <div class="ec-health">
            <span class="hl ${h.level || "empty"}">${esc(h.level_label || "—")}</span>
            <span class="ec-nums">${h.total ? `共 ${h.total} · 活 ${h.alive} · 挂 ${h.offline}` : "未登记实例"}
              ${h.total ? `· 24h 重启 ${h.restarts_24h}` : ""}</span>
          </div>
          <div class="ec-window" style="color:var(--ink-3)">
            封网日 ${(e.holidays || []).filter((x) => !x.passed).length} 天 · 配置 ${e.usage.config_items} 项 · 实例 ${e.usage.instances} 个
          </div>
          <div class="ec-actions">
            ${canManage ? `<button class="btn small" data-window="${e.id}">设置窗口 / 封网</button>` : ""}
            <button class="btn small" data-health="${e.id}" data-env="${esc(e.env_key)}">看实例健康</button>
            ${canManage ? (e.can_delete
                ? `<button class="btn small danger" data-del="${e.id}">删除环境</button>`
                : `<button class="btn small danger" disabled title="${esc((e.delete_blockers || []).join("；"))}">删除环境</button>`)
              : ""}
          </div>
        </div>`;
      }).join("")}
    </div>`;

  $$("[data-window]", box).forEach((b) => {
    b.onclick = () => openWindowModal(app, envs.find((x) => x.id === +b.dataset.window));
  });
  $$("[data-health]", box).forEach((b) => {
    b.onclick = () => { $("#modal-root").innerHTML = ""; location.hash = `#/health/${app.id}/${b.dataset.env}`; };
  });
  $$("[data-del]", box).forEach((b) => {
    b.onclick = () => deleteEnv(app, envs.find((x) => x.id === +b.dataset.del));
  });

  const addBox = $("#em-add");
  if (canManage) {
    addBox.innerHTML = `
      <div class="panel panel-pad" style="background:#fafbfc">
        <b style="font-size:13px">新增自定义环境</b>
        <div class="filter-bar" style="box-shadow:none;border:0;padding:8px 0 0">
          <input id="em-key" placeholder="标识（小写英文，如 gray / pre-prod）" style="width:230px">
          <input id="em-label" placeholder="名称（如 灰度 / 预演）" style="width:160px">
          <button class="btn primary small" id="em-create">+ 新增环境</button>
          <span style="color:var(--ink-3);font-size:12px">新环境默认全时段允许发布，可随后设置窗口</span>
        </div>
        <div class="form-error" id="em-err"></div>
      </div>`;
    $("#em-create").onclick = async () => {
      const errBox = $("#em-err");
      errBox.classList.remove("show");
      try {
        await api(`/api/apps/${app.id}/environments`, {
          method: "POST",
          body: { env_key: $("#em-key").value.trim(), env_label: $("#em-label").value.trim() },
        });
        toast("环境已新增", "success");
        paintEnvList(app);
      } catch (e2) { errBox.textContent = e2.message; errBox.classList.add("show"); }
    };
  }
}

async function deleteEnv(app, env) {
  if (!confirm(`确认删除环境「${env.env_label}」？删除前会再次检查是否有配置/版本/实例挂载。`)) return;
  try {
    await api(`/api/apps/${app.id}/environments/${env.id}`, { method: "DELETE" });
    toast(`环境「${env.env_label}」已删除`, "success");
    paintEnvList(app);
  } catch (e) {
    // 有挂载：明确说出挂在哪里，而不是只让按钮置灰
    const blockers = e.payload && e.payload.blockers ? e.payload.blockers : [e.message];
    openDeleteBlockedModal(env, blockers);
  }
}

function openDeleteBlockedModal(env, blockers) {
  const root = $("#modal-root");
  // 覆盖式弹到最上层（环境管理弹窗之上）
  root.insertAdjacentHTML("beforeend", `
    <div class="modal-mask" id="db-mask"><div class="modal" style="width:520px">
      <h3>无法删除环境「${esc(env.env_label)}」</h3>
      <p style="color:var(--ink-2);font-size:13px">该环境上还挂着资产，必须先迁移或清理后才能删除：</p>
      <ul style="line-height:1.9;color:#a02a2e">
        ${blockers.map((b) => `<li>${esc(b)}</li>`).join("")}
      </ul>
      <div class="form-actions"><button class="btn primary" id="db-ok">我知道了</button></div>
    </div></div>`);
  $("#db-ok").onclick = () => { $("#db-mask").remove(); };
  $("#db-mask").onclick = (ev) => { if (ev.target.id === "db-mask") $("#db-mask").remove(); };
}

function openWindowModal(app, env, onSaved) {
  const root = $("#modal-root");
  let days = new Set(env.window_days || []);
  let restricted = env.deploy_restricted;
  let start = env.window_start || "10:00";
  let end = env.window_end || "18:00";

  root.insertAdjacentHTML("beforeend", `
    <div class="modal-mask" id="win-mask"><div class="modal" style="width:600px">
      <h3>发布窗口 · ${esc(env.env_label)}环境</h3>
      <label class="chk" style="margin-bottom:10px">
        <input type="checkbox" id="w-restrict" ${restricted ? "checked" : ""}>
        限制发布时间（不勾选 = 全时段允许发布）
      </label>
      <div id="w-body">
        <label style="font-size:12px;color:var(--ink-2)">允许发布的星期</label>
        <div class="week-days" id="w-days" style="margin:6px 0 12px">
          ${WEEK_NAMES.map((n, i) => `<div class="week-day ${days.has(i) ? "on" : ""}" data-d="${i}">${n}</div>`).join("")}
        </div>
        <label style="font-size:12px;color:var(--ink-2)">每日允许时段</label>
        <div class="time-range" style="margin:6px 0 4px">
          <input type="time" id="w-start" value="${esc(start)}">
          <span>至</span>
          <input type="time" id="w-end" value="${esc(end)}">
        </div>
        <p style="color:var(--ink-3);font-size:12px;margin:2px 0 0">窗口外（含非发布日）的上线会被拦下，并提示下一次开放时间；暂不支持跨午夜窗口。</p>
      </div>
      <div class="form-error" id="w-err"></div>
      <div class="form-actions">
        <button class="btn" id="w-cancel">取消</button>
        <button class="btn primary" id="w-save">保存窗口</button>
      </div>

      <h3 style="margin-top:18px;font-size:14px">节假日封网</h3>
      <p style="color:var(--ink-3);font-size:12px;margin:0 0 8px">命中以下日期时，即使是发布日也整天关闭发布。</p>
      <div class="filter-bar" style="box-shadow:none;border:0;padding:0">
        <input type="date" id="h-date">
        <input id="h-reason" class="grow" placeholder="封网原因（如 国庆节 / 大促冻结）">
        <button class="btn small" id="h-add">+ 新增封网日</button>
      </div>
      <div class="holiday-list" id="h-list"></div>
      <div class="form-error" id="h-err"></div>
    </div></div>`);
  const mask = $("#win-mask");
  const close = () => mask.remove();
  $("#w-cancel").onclick = close;
  mask.onclick = (ev) => { if (ev.target === mask) close(); };

  function syncBody() { $("#w-body").style.opacity = restricted ? "1" : ".45"; $("#w-body").style.pointerEvents = restricted ? "auto" : "none"; }
  syncBody();
  $("#w-restrict").onchange = (ev) => { restricted = ev.target.checked; syncBody(); };
  $$(".week-day", mask).forEach((d) => {
    d.onclick = () => {
      const k = +d.dataset.d;
      if (days.has(k)) { days.delete(k); d.classList.remove("on"); }
      else { days.add(k); d.classList.add("on"); }
    };
  });

  $("#w-save").onclick = async () => {
    const box = $("#w-err");
    box.classList.remove("show");
    try {
      const saved = await api(`/api/apps/${app.id}/environments/${env.id}/window`, {
        method: "PUT",
        body: {
          deploy_restricted: restricted,
          window_days: [...days].sort((a, b) => a - b),
          window_start: $("#w-start").value || "10:00",
          window_end: $("#w-end").value || "18:00",
        },
      });
      toast("发布窗口已更新并留痕", "success");
      env = saved; days = new Set(saved.window_days); restricted = saved.deploy_restricted;
      start = saved.window_start; end = saved.window_end;
      paintHolidays();
      if (onSaved) onSaved();
    } catch (e) { box.textContent = e.message; box.classList.add("show"); }
  };

  $("#h-add").onclick = async () => {
    const box = $("#h-err");
    box.classList.remove("show");
    try {
      await api(`/api/apps/${app.id}/environments/${env.id}/holidays`, {
        method: "POST",
        body: { date: $("#h-date").value, reason: $("#h-reason").value.trim() },
      });
      toast("已新增封网日并留痕", "success");
      $("#h-date").value = ""; $("#h-reason").value = "";
      paintHolidays();
      if (onSaved) onSaved();
    } catch (e) { box.textContent = e.message; box.classList.add("show"); }
  };

  async function paintHolidays() {
    const fresh = await api(`/api/apps/${app.id}/environments/${env.id}/window`);
    env.holidays = fresh.holidays;
    const list = $("#h-list");
    if (!fresh.holidays.length) { list.innerHTML = '<div class="empty-tip" style="padding:10px 0">暂无封网日</div>'; return; }
    list.innerHTML = fresh.holidays.map((h) => `
      <div class="holiday-row ${h.passed ? "passed" : ""}">
        <span class="h-date">${esc(h.date)}</span>
        <span class="h-reason">${esc(h.reason || "节假日封网")}</span>
        ${h.passed ? '<span style="color:var(--ink-3);font-size:11px">（已过）</span>' : ""}
        <button class="btn small" data-rmh="${h.id}">解除</button>
      </div>`).join("");
    $$("[data-rmh]", list).forEach((b) => {
      b.onclick = async () => {
        try {
          await api(`/api/apps/${app.id}/environments/${env.id}/holidays/${b.dataset.rmh}`, { method: "DELETE" });
          toast("已解除封网并留痕", "success"); paintHolidays();
          if (onSaved) onSaved();
        } catch (e) { toast(e.message, "error"); }
      };
    });
  }
  paintHolidays();
}

/* ---------------- 应用健康（跨应用作战台 / 单应用环境） ---------------- */
async function renderHealthBoard() {
  const view = $("#view");
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>应用健康</h2>
        <div class="sub">各应用各环境的实例存活与重启频率；掉线与频繁重启直接分级标出</div>
      </div>
    </div>
    <div class="health-summary">
      <button class="health-pill active" data-level="">全部异常</button>
      <button class="health-pill" data-level="offline">实例掉线</button>
      <button class="health-pill" data-level="critical">频繁重启</button>
      <button class="health-pill" data-level="warning">重启偏多</button>
      <button class="health-pill" data-level="normal">健康</button>
    </div>
    <select id="hb-bl" ${state.user.role === "admin" ? "" : "disabled"} style="margin-bottom:14px">
      <option value="">全部业务线</option>
      ${state.businessLines.map((b) => `<option value="${b.id}">${esc(b.name)}</option>`).join("")}
    </select>
    <div id="hb-result"><div class="empty-tip">加载中…</div></div>`;
  let level = "";
  const load = async () => {
    const bl = $("#hb-bl").value;
    const params = new URLSearchParams();
    if (bl) params.set("business_line_id", bl);
    let cards;
    try {
      cards = await api(`/api/health/overview?${params.toString()}`);
    } catch (e) { $("#hb-result").innerHTML = errorStateHtml("加载失败", e.message); return; }
    const shown = level ? cards.filter((c) => c.health.level === level)
                        : cards.filter((c) => ["offline", "critical", "warning"].includes(c.health.level));
    const box = $("#hb-result");
    if (!shown.length) {
      box.innerHTML = `<div class="panel panel-pad empty-tip">${level === "normal" ? "没有完全健康的环境" : "当前筛选下没有异常环境"}</div>`;
      return;
    }
    box.innerHTML = `
      <div class="panel table-wrap">
        <table class="app-table">
          <thead><tr><th>业务线</th><th>应用</th><th>环境</th><th>健康状态</th>
            <th>实例(总/活/挂)</th><th>近24h重启</th><th>近7天异常实例</th><th>最近重启</th></tr></thead>
          <tbody>
            ${shown.map((c) => {
              const h = c.health;
              return `<tr data-app="${c.app_id}" data-env="${esc(c.environment)}">
                <td>${esc(c.business_line_name)}</td>
                <td class="app-name-cell">${esc(c.app_name)}
                  ${c.app_status === "offline" ? '<span class="badge offline">已下线</span>' : ""}</td>
                <td>${envTagHtml(c.environment, c.environment_label)}</td>
                <td><span class="hl ${h.level}">${esc(h.level_label)}</span></td>
                <td>${h.total} / ${h.alive} / <b class="${h.offline ? "r24-crit" : ""}">${h.offline}</b></td>
                <td class="inst-restarts"><span class="${h.restarts_24h >= 5 ? "r24-crit" : h.restarts_24h >= 2 ? "r24-warn" : "r-normal"}">${h.restarts_24h} 次</span></td>
                <td>${h.critical_count ? `<span class="r24-crit">频繁 ${h.critical_count}</span>` : ""}${h.warning_count ? ` <span class="r24-warn">偏多 ${h.warning_count}</span>` : ""}${!h.critical_count && !h.warning_count ? '<span class="r-normal">—</span>' : ""}</td>
                <td>${h.last_restart_at ? fmtAgo(h.last_restart_at) : "—"}</td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
      <p style="color:var(--ink-3);font-size:12px;margin:8px 2px">
        分级：近 24 小时重启 ≥5 次为「频繁重启」（红），≥2 次或近 7 天 ≥7 次为「重启偏多」（黄）；半个月一次的正常重启不在此列。
      </p>`;
    $$("#hb-result [data-app]", box).forEach((tr) => {
      tr.onclick = () => { location.hash = `#/health/${tr.dataset.app}/${tr.dataset.env}`; };
    });
  };
  $$(".health-pill", view).forEach((p) => {
    p.onclick = () => {
      $$(".health-pill", view).forEach((x) => x.classList.toggle("active", x === p));
      level = p.dataset.level; load();
    };
  });
  $("#hb-bl").onchange = load;
  await load();
}

async function renderAppHealth(appId, envKey) {
  const view = $("#view");
  view.innerHTML = `<div class="empty-tip">加载中…</div>`;
  let app, health;
  try {
    [app, health] = await Promise.all([
      api(`/api/apps/${appId}`),
      api(`/api/apps/${appId}/health`),
    ]);
  } catch (e) {
    view.innerHTML = errorStateHtml(e.status === 403 ? "403 无权访问" : "加载失败", e.message);
    return;
  }
  const envs = health.environments;
  let curKey = envKey && envs.some((e) => e.environment === envKey)
    ? envKey : (envs[0] ? envs[0].environment : "");
  const canManage = !!app.can_manage && app.status !== "offline";

  const paint = () => {
    const cur = envs.find((e) => e.environment === curKey) || envs[0];
    const h = cur ? cur.health : { total: 0, alive: 0, offline: 0, instances: [], level: "empty", level_label: "未登记实例", restarts_24h: 0 };
    const w = cur ? cur.window_status : null;
    view.innerHTML = `
      <div class="page-head">
        <div>
          <h2>${esc(app.name)} · 应用健康</h2>
          <div class="sub">${esc(app.business_line_name)} · 实例级存活 / 掉线 / 重启监控</div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn" id="ah-board">← 健康总览</button>
          ${canManage ? '<button class="btn" id="ah-env">环境与发布窗口</button>' : ""}
          <button class="btn" id="ah-app">查看应用</button>
        </div>
      </div>
      <div class="env-tabs">
        ${envs.map((e) => {
          const lv = e.health.level;
          return `<a class="env-tab ${e.environment === curKey ? "active" : ""} ${e.environment === "prod" ? "prod" : ""}"
            href="#/health/${appId}/${esc(e.environment)}">${esc(e.env_label)}
            ${lv === "offline" ? ' 🔴' : lv === "critical" ? ' ⚠️' : ""}</a>`;
        }).join("")}
      </div>

      ${cur ? (w && w.restricted && !w.allowed ? `
        <div class="window-bar closed">
          <span>🚫 ${esc(w.message)}</span>
          ${canManage ? `<span class="wb-actions"><button class="btn small" id="ah-window">调整窗口</button></span>` : ""}
        </div>` : `
        <div class="window-bar ${w && w.allowed ? "open" : "closed"}">
          <span>${w && w.allowed ? "🟢 发布窗口开放中" : "🕒"} ${esc(w ? w.window_text : "")}</span>
          ${w && w.next_close_at ? `<span class="wb-detail">今天 ${fmtClock(w.next_close_at)} 关闭</span>` : ""}
          ${canManage ? `<span class="wb-actions"><button class="btn small" id="ah-window">设置窗口 / 封网</button></span>` : ""}
        </div>`) : ""}

      <div class="panel panel-pad" style="margin-bottom:14px">
        <div class="cfg-toolbar">
          <h3 style="margin:0">${esc(cur ? cur.env_label : "")}环境实例
            <span class="hl ${h.level}" style="margin-left:8px">${esc(h.level_label)}</span>
          </h3>
          <div style="display:flex;gap:14px;align-items:center;font-size:13px;color:var(--ink-2)">
            <span>共 <b>${h.total}</b></span>
            <span>存活 <b style="color:var(--green)">${h.alive}</b></span>
            <span>掉线 <b style="color:${h.offline ? "var(--red)" : "inherit"}">${h.offline}</b></span>
            <span>近24h重启 <b class="${h.restarts_24h >= 5 ? "r24-crit" : h.restarts_24h >= 2 ? "r24-warn" : ""}">${h.restarts_24h}</b></span>
            ${canManage && cur ? `<button class="btn primary small" id="ah-add-inst">+ 登记实例</button>` : ""}
          </div>
        </div>
        ${paintInstanceTable(app, cur, h.instances || [], canManage)}
      </div>`;

    $("#ah-board").onclick = () => { location.hash = "#/health"; };
    $("#ah-app").onclick = () => { location.hash = `#/apps/${appId}`; };
    const winBtn = $("#ah-window");
    if (winBtn) winBtn.onclick = () => openWindowModalFromHealth(app, cur, reloadHealth);
    const envBtn = $("#ah-env");
    if (envBtn) envBtn.onclick = () => openEnvManagerModal(app);
    const addBtn = $("#ah-add-inst");
    if (addBtn) addBtn.onclick = () => openAddInstanceModal(app, cur, () => reloadHealth());
    bindInstanceActions(app, cur, canManage, () => reloadHealth());
  };

  async function reloadHealth() {
    health = await api(`/api/apps/${appId}/health`);
    envs.length = 0;
    health.environments.forEach((e) => envs.push(e));
    paint();
  }
  paint();
}

function fmtClock(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function paintInstanceTable(app, env, instances, canManage) {
  if (!env) return `<div class="empty-tip">没有可见环境</div>`;
  if (!instances.length) {
    return `<div class="empty-tip">该环境还没有登记实例。${canManage ? "点右上角「登记实例」开始纳管。" : ""}</div>`;
  }
  return `
    <div class="table-wrap">
      <table class="app-table inst-table">
        <thead><tr><th>实例</th><th>状态</th><th>累计重启</th><th>近24h</th><th>近7天</th>
          <th>最后重启</th><th>最近存活</th><th>事件</th><th style="width:150px">操作</th></tr></thead>
        <tbody>
          ${instances.map((i) => {
            const rc = i.restarts_24h >= 5 ? "r24-crit" : i.restarts_24h >= 2 ? "r24-warn" : "r-normal";
            const r7c = i.restarts_7d >= 7 ? "r24-warn" : "r-normal";
            return `<tr class="${i.status === "offline" ? "" : ""}">
              <td class="mono app-name-cell">${esc(i.name)}</td>
              <td><span class="inst-status"><span class="dot ${i.status}"></span>${esc(i.status_label)}</span>
                ${i.status === "offline" && i.offline_since ? `<div class="app-desc">掉线 ${fmtAgo(i.offline_since)}</div>` : ""}</td>
              <td>${i.restart_count} 次</td>
              <td class="inst-restarts"><span class="${rc}">${i.restarts_24h}</span></td>
              <td class="inst-restarts"><span class="${r7c}">${i.restarts_7d}</span></td>
              <td>${i.last_restart_at ? fmtAgo(i.last_restart_at) : '<span class="r-normal">从未</span>'}</td>
              <td>${fmtAgo(i.last_seen_at)}</td>
              <td><button class="btn small" data-events="${i.id}">事件流</button></td>
              <td>${canManage ? (i.status === "offline"
                  ? `<button class="btn small" data-act="recover" data-id="${i.id}">恢复</button>`
                  : `<button class="btn small" data-act="restart" data-id="${i.id}">重启</button>
                     <button class="btn small danger" data-act="offline" data-id="${i.id}">模拟掉线</button>`)
                : '<span class="r-normal">—</span>'}</td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>
    </div>
    <p style="color:var(--ink-3);font-size:12px;margin:10px 2px 0">
      近 24h 重启数 <b class="r24-crit">≥5</b> 标红（频繁重启），<b class="r24-warn">≥2</b> 或近 7 天 <b class="r24-warn">≥7</b> 标黄；
      重启/掉线/恢复都会写入事件流与变更留痕。
    </p>`;
}

function bindInstanceActions(app, env, canManage, onChange) {
  if (!env) return;
  $$("[data-events]").forEach((b) => {
    b.onclick = () => openEventModal(app, +b.dataset.events);
  });
  if (!canManage) return;
  $$("[data-act]").forEach((b) => {
    b.onclick = async () => {
      const act = b.dataset.act, id = b.dataset.id;
      if (act === "offline" && !confirm("确认将该实例标记为掉线？会写入掉线事件与留痕，并在作战台冒出告警。")) return;
      try {
        await api(`/api/apps/${app.id}/instances/${id}/${act === "recover" ? "recover" : act}`, {
          method: "POST", body: { detail: "" },
        });
        toast({ restart: "已记录一次重启", offline: "实例已标记掉线", recover: "实例已恢复存活" }[act], "success");
        onChange();
      } catch (e) { toast(e.message, "error"); }
    };
  });
}

async function openEventModal(app, instanceId) {
  let events;
  try {
    events = await api(`/api/apps/${app.id}/instances/${instanceId}/events`);
  } catch (e) { toast(e.message, "error"); return; }
  const root = $("#modal-root");
  root.insertAdjacentHTML("beforeend", `
    <div class="modal-mask" id="ev-mask"><div class="modal" style="width:560px">
      <h3>实例事件流</h3>
      <div class="event-list">
        ${events.length ? events.map((e) => `
          <div class="event-row ${e.event_type}">
            <span class="ev-time">${fmtTime(e.created_at)}</span>
            <span class="ev-type">${esc(e.event_label)}</span>
            <span>${esc(e.detail || "")}</span>
            <span class="ai-meta" style="margin-left:auto">${esc(e.actor_name)}</span>
          </div>`).join("") : '<div class="empty-tip">暂无事件</div>'}
      </div>
      <div class="form-actions"><button class="btn primary" id="ev-done">关闭</button></div>
    </div></div>`);
  $("#ev-done").onclick = () => $("#ev-mask").remove();
  $("#ev-mask").onclick = (ev) => { if (ev.target.id === "ev-mask") $("#ev-mask").remove(); };
}

function openAddInstanceModal(app, env, onChange) {
  const root = $("#modal-root");
  root.insertAdjacentHTML("beforeend", `
    <div class="modal-mask" id="ai-mask"><div class="modal" style="width:460px">
      <h3>登记实例 · ${esc(env.env_label)}环境</h3>
      <div class="form-grid" style="grid-template-columns:1fr">
        <div><label>实例名 *</label>
          <input id="ai-name" placeholder="如 pay-gw-7d9c4-pod-xxxx（集群内唯一）"></div>
      </div>
      <div class="form-error" id="ai-err"></div>
      <div class="form-actions">
        <button class="btn" id="ai-cancel">取消</button>
        <button class="btn primary" id="ai-ok">登记（初始存活）</button>
      </div>
    </div></div>`);
  const mask = $("#ai-mask");
  $("#ai-cancel").onclick = () => mask.remove();
  mask.onclick = (ev) => { if (ev.target === mask) mask.remove(); };
  $("#ai-ok").onclick = async () => {
    const box = $("#ai-err");
    box.classList.remove("show");
    try {
      await api(`/api/apps/${app.id}/instances`, {
        method: "POST", body: { environment: env.environment, name: $("#ai-name").value.trim() },
      });
      toast("实例已登记（初始存活）", "success");
      mask.remove(); onChange();
    } catch (e) { box.textContent = e.message; box.classList.add("show"); }
  };
}

async function openWindowModalFromHealth(app, env, onChange) {
  // 健康页只拿到 window/health，窗口编辑需要 environments 详情（usage/holidays/can_delete）
  const envs = await api(`/api/apps/${app.id}/environments`);
  const full = envs.find((x) => x.env_key === env.environment);
  openWindowModal(app, full, onChange);
}

/* ---------------- 数据同步中心 ---------------- */
let syncTab = "overview";

const SYNC_RESULT_STYLE = {
  created: { cls: "online", text: "新增" },
  updated: { cls: "developing", text: "改动" },
  unchanged: { cls: "offline", text: "无变化" },
  conflict: { cls: "maintenance", text: "两边改动待裁决" },
  failed: { cls: "", text: "未通过" },
  local_deleted: { cls: "", text: "本地已删·拒收复活" },
  upstream_deleted: { cls: "", text: "上游已删·待处理" },
};

function syncResultBadge(result, label) {
  const s = SYNC_RESULT_STYLE[result] || { cls: "offline", text: result };
  const extra = (result === "failed" || result === "local_deleted")
    ? "background:#fde8e8;color:#c02f33"
    : result === "upstream_deleted" ? "background:#fdf3d7;color:#ad7209" : "";
  return `<span class="badge ${s.cls}" style="${extra}">${esc(label || s.text)}</span>`;
}

function fmtDuration(ms) {
  if (ms == null) return "-";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

async function renderSync() {
  if (state.user.role !== "admin") {
    $("#view").innerHTML = errorStateHtml("403", "数据同步是平台级能力（跨全部业务线），仅平台管理员可操作与查看");
    return;
  }
  const view = $("#view");
  view.innerHTML = `<div class="empty-tip">加载中…</div>`;
  let st;
  try {
    st = await api("/api/sync/status");
  } catch (e) {
    view.innerHTML = errorStateHtml("加载失败", e.message);
    return;
  }
  state.syncStatus = st;
  const tabs = [
    ["overview", "同步台"],
    ["conflicts", `冲突裁决${st.pending_conflicts ? `（${st.pending_conflicts}）` : ""}`],
    ["deletions", `上游删除${st.pending_remote_deletions ? `（${st.pending_remote_deletions}）` : ""}`],
    ["tombstones", "本地删除记录"],
    ["audit", "同步留痕"],
  ];
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>数据同步</h2>
        <div class="sub">上游周期性推送应用 / 模块 / 环境；定时按间隔跑，也可手动立刻跑一趟</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn" id="btn-sync-reset" title="清空同步实体与队列，恢复初始演示报文">重置演示数据</button>
        <button class="btn primary" id="btn-sync-now">⟳ 立即同步一趟</button>
      </div>
    </div>
    ${st.pending_conflicts || st.pending_remote_deletions ? `
    <div class="panel panel-pad" style="margin-bottom:14px;border-color:#f0d8a0;background:#fffaf0">
      ${st.pending_conflicts ? `<p style="margin:0 0 ${st.pending_remote_deletions ? 8 : 0}px">
        <span class="red-dot">有 ${st.pending_conflicts} 条两边都改过的记录待裁决</span>
        —— 后到的不会盖掉先到的，<a href="javascript:void(0)" id="go-conflict">去裁决</a></p>` : ""}
      ${st.pending_remote_deletions ? `<p style="margin:0">
        <span class="red-dot">有 ${st.pending_remote_deletions} 条上游已删但本地仍挂着的记录待处理</span>
        —— <a href="javascript:void(0)" id="go-deletion">去处理</a></p>` : ""}
    </div>` : ""}
    <div class="admin-tabs">
      ${tabs.map(([k, t]) => `<button class="adm-tab ${syncTab === k ? "active" : ""}" data-st="${k}">${t}</button>`).join("")}
    </div>
    <div id="sync-body"><div class="empty-tip">加载中…</div></div>`;

  $$(".adm-tab", view).forEach((b) => {
    b.onclick = () => { syncTab = b.dataset.st; renderSync(); };
  });
  $("#btn-sync-now").onclick = () => runSyncNow();
  $("#btn-sync-reset").onclick = () => resetSyncDemo();
  if ($("#go-conflict")) $("#go-conflict").onclick = () => { syncTab = "conflicts"; renderSync(); };
  if ($("#go-deletion")) $("#go-deletion").onclick = () => { syncTab = "deletions"; renderSync(); };

  if (syncTab === "overview") paintSyncOverview(st);
  else if (syncTab === "conflicts") paintSyncConflicts();
  else if (syncTab === "deletions") paintSyncDeletions();
  else if (syncTab === "tombstones") paintSyncTombstones();
  else paintSyncAudit();
}

async function runSyncNow(scene) {
  const btn = $("#btn-sync-now");
  if (btn) { btn.disabled = true; btn.textContent = "同步执行中…"; }
  try {
    const r = await api("/api/sync/run", { method: "POST", body: { source_scene: scene || null } });
    const warn = r.status === "partial";
    toast(`同步完成：共 ${r.total} 条 · 新增 ${r.created_count} · 改动 ${r.updated_count} · `
      + `待裁决 ${r.conflict_count} · 未通过 ${r.failed_count} · 拒收复活 ${r.local_deleted_count} · `
      + `上游已删 ${r.upstream_deleted_count}`, warn ? "error" : "success");
    syncTab = "overview";
    renderSync();
    if (warn) setTimeout(() => openRunModal(r.run_id), 300);
  } catch (e) {
    toast(e.message, "error");
    if (btn) { btn.disabled = false; btn.textContent = "⟳ 立即同步一趟"; }
  }
}

async function resetSyncDemo() {
  openConfirmModal({
    title: "重置同步演示数据",
    message: "将删除所有 ext- 前缀的同步应用/模块/环境，清空运行记录、冲突、上游删除与墓碑队列，并重新生成初始上游报文。确定继续？",
    confirmText: "重置", danger: true,
    onConfirm: async () => {
      try {
        await api("/api/sync/demo/reset", { method: "POST" });
        toast("演示数据已重置", "success");
        renderSync();
      } catch (e) { toast(e.message, "error"); }
    },
  });
}

/* ---------- 同步台（设置 + 最近一趟 + 历史） ---------- */
async function paintSyncOverview(st) {
  const body = $("#sync-body");
  const s = st.settings;
  const lr = st.last_run;
  body.innerHTML = `
    <div class="dash-grid" style="margin-bottom:16px">
      <div class="panel panel-pad">
        <h3>定时同步设置</h3>
        <div class="form-grid" style="gap:12px">
          <label class="kv">
            <span class="k">定时开关</span>
            <span class="v" style="display:flex;align-items:center;gap:8px">
              <input type="checkbox" id="sy-enabled" ${s.enabled ? "checked" : ""}>
              <span>${s.enabled ? "已开启（后台按间隔自动跑）" : "已关闭（仅手动触发）"}</span>
            </span>
          </label>
          <label class="kv">
            <span class="k">同步间隔（秒，30 ~ 86400）</span>
            <span class="v"><input type="number" id="sy-interval" value="${s.interval_seconds}" min="30" max="86400" style="width:140px"></span>
          </label>
          <label class="kv">
            <span class="k">模拟上游源剧本</span>
            <span class="v"><select id="sy-scene" style="min-width:200px">
              ${st.scenes.map((x) => `<option value="${x.value}" ${s.source_scene === x.value ? "selected" : ""}>${esc(x.label)}（${x.value}）</option>`).join("")}
            </select></span>
          </label>
          <div><button class="btn primary" id="sy-save">保存设置</button>
            <span style="color:var(--ink-3);font-size:12px;margin-left:8px">手动"立即同步"可临时选剧本试跑，不改这里的设置</span></div>
        </div>
      </div>
      <div class="panel panel-pad">
        <h3>最近一趟同步</h3>
        ${lr ? `
        <div class="kv-grid">
          <div class="kv"><div class="k">状态</div><div class="v">${esc(lr.status_label)}</div></div>
          <div class="kv"><div class="k">触发方式 / 触发人</div><div class="v">${esc(lr.trigger_label)} · ${esc(lr.triggered_by_name)}</div></div>
          <div class="kv"><div class="k">开始时间</div><div class="v">${fmtTime(lr.started_at)}</div></div>
          <div class="kv"><div class="k">耗时</div><div class="v">${fmtDuration(lr.duration_ms)}</div></div>
          <div class="kv"><div class="k">模拟源</div><div class="v">${esc(lr.scene_label)}</div></div>
          <div class="kv"><div class="k">待处理</div><div class="v">冲突 ${st.pending_conflicts} · 上游删除 ${st.pending_remote_deletions}</div></div>
        </div>
        ${lr.error_message ? `<p style="color:var(--red);margin:10px 0 0">${esc(lr.error_message)}</p>` : ""}`
        : '<div class="empty-tip">还没有跑过同步，点右上角"立即同步一趟"</div>'}
      </div>
    </div>
    ${lr ? `
    <div class="panel panel-pad" style="margin-bottom:16px">
      <h3>最近一趟结果（运行 #${lr.id} · 共 ${lr.total_count} 条）</h3>
      <div class="stats-grid" style="grid-template-columns:repeat(7,1fr)">
        ${[
          ["新增", lr.created_count], ["改动", lr.updated_count], ["无变化", lr.unchanged_count],
          ["两边改动待裁决", lr.conflict_count], ["未通过", lr.failed_count],
          ["拒收复活", lr.local_deleted_count], ["上游已删待处理", lr.upstream_deleted_count],
        ].map(([l, v]) => `<div class="panel stat-card" style="box-shadow:none">
          <div class="num ${v ? "" : ""}" style="font-size:22px">${v}</div><div class="label">${l}</div></div>`).join("")}
      </div>
      <div style="margin-top:10px"><button class="btn small" id="sy-open-last">查看本趟逐条明细</button></div>
    </div>` : ""}
    <div class="panel panel-pad">
      <h3>历史运行</h3>
      <div id="sy-runs"><div class="empty-tip">加载中…</div></div>
    </div>`;

  $("#sy-save").onclick = async () => {
    try {
      await api("/api/sync/settings", {
        method: "PUT",
        body: {
          enabled: $("#sy-enabled").checked,
          interval_seconds: parseInt($("#sy-interval").value, 10),
          source_scene: $("#sy-scene").value,
        },
      });
      toast("同步设置已保存", "success");
      renderSync();
    } catch (e) { toast(e.message, "error"); }
  };
  if ($("#sy-open-last")) $("#sy-open-last").onclick = () => openRunModal(lr.id);
  try {
    const runs = await api("/api/sync/runs?limit=20");
    $("#sy-runs").innerHTML = runs.length ? `
      <div class="table-wrap"><table class="app-table">
        <thead><tr><th>#</th><th>状态</th><th>触发</th><th>模拟源</th><th>时间</th><th>耗时</th>
        <th>新增</th><th>改动</th><th>待裁决</th><th>未通过</th><th>拒收复活</th><th>上游已删</th><th></th></tr></thead>
        <tbody>${runs.map((r) => `
          <tr data-run="${r.id}">
            <td>${r.id}</td>
            <td>${esc(r.status_label)}${r.error_message ? `<div class="app-desc" style="color:var(--red)">${esc(r.error_message)}</div>` : ""}</td>
            <td>${esc(r.trigger_label)}<div class="app-desc">${esc(r.triggered_by_name)}</div></td>
            <td>${esc(r.scene_label)}</td>
            <td>${fmtTime(r.started_at)}</td>
            <td>${fmtDuration(r.duration_ms)}</td>
            <td>${r.created_count}</td><td>${r.updated_count}</td>
            <td>${r.conflict_count ? `<b style="color:var(--amber)">${r.conflict_count}</b>` : 0}</td>
            <td>${r.failed_count ? `<b style="color:var(--red)">${r.failed_count}</b>` : 0}</td>
            <td>${r.local_deleted_count}</td><td>${r.upstream_deleted_count}</td>
            <td><button class="btn small">明细</button></td>
          </tr>`).join("")}
        </tbody></table></div>`
      : '<div class="empty-tip">暂无运行记录</div>';
    $$("#sy-runs tbody tr").forEach((tr) => {
      tr.onclick = () => openRunModal(parseInt(tr.dataset.run, 10));
    });
  } catch (e) {
    $("#sy-runs").innerHTML = errorStateHtml("加载失败", e.message);
  }
}

/* ---------- 运行明细弹窗 ---------- */
async function openRunModal(runId) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-mask"><div class="modal" style="width:920px">
    <h3>同步运行 #${runId} 明细</h3><div id="rm-body"><div class="empty-tip">加载中…</div></div>
    <div style="text-align:right;margin-top:14px"><button class="btn" id="rm-close">关闭</button></div>
  </div></div>`;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) root.innerHTML = ""; };
  $("#rm-close").onclick = () => { root.innerHTML = ""; };
  let r;
  try { r = await api(`/api/sync/runs/${runId}`); }
  catch (e) { $("#rm-body").innerHTML = errorStateHtml("加载失败", e.message); return; }

  const groups = [
    ["failed", "未通过（卡住原因）"], ["conflict", "两边改动·待裁决"],
    ["upstream_deleted", "上游已删·待处理"], ["local_deleted", "本地已删·拒收复活"],
    ["created", "新增"], ["updated", "改动"], ["unchanged", "无变化"],
  ].map(([res, label]) => ({ res, label, rows: r.items.filter((i) => i.result === res) }))
   .filter((g) => g.rows.length);

  $("#rm-body").innerHTML = `
    <div style="color:var(--ink-2);font-size:13px;margin-bottom:10px">
      ${esc(r.status_label)} · ${esc(r.trigger_label)}（${esc(r.triggered_by_name)}）·
      ${fmtTime(r.started_at)} · 耗时 ${fmtDuration(r.duration_ms)} · 模拟源：${esc(r.scene_label)}
      ${r.error_message ? ` · <span style="color:var(--red)">${esc(r.error_message)}</span>` : ""}
    </div>
    ${groups.map((g) => `
      <p style="margin:12px 0 6px"><b>${esc(g.label)}（${g.rows.length}）</b></p>
      <div class="table-wrap"><table class="env-table">
        <thead><tr><th>类型</th><th>名称</th><th>结果</th><th>说明 / 卡在哪</th><th></th></tr></thead>
        <tbody>${g.rows.map((it) => `
          <tr>
            <td>${esc(it.entity_label)}</td>
            <td>${esc(it.name)}<div class="app-desc mono">${esc(it.external_key)}</div></td>
            <td>${syncResultBadge(it.result, it.result_label)}</td>
            <td style="white-space:normal;max-width:420px">${esc(it.reason || "—")}</td>
            <td>${it.result === "conflict" ? `<button class="btn small" data-conflict-from-run="1">看差异/裁决</button>` : ""}</td>
          </tr>`).join("")}
        </tbody></table></div>`).join("") || '<div class="empty-tip">本趟没有条目</div>'}`;
  $$("[data-conflict-from-run]", root).forEach((b) => {
    b.onclick = () => {
      root.innerHTML = "";
      syncTab = "conflicts";
      renderSync();
    };
  });
}

/* ---------- 冲突裁决 ---------- */
async function paintSyncConflicts() {
  const body = $("#sync-body");
  body.innerHTML = `
    <div class="panel panel-pad">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <h3 style="margin:0">两边改动待裁决</h3>
        <select id="sc-filter" style="width:160px">
          <option value="pending">待裁决</option>
          <option value="all">全部（含已裁决留痕）</option>
        </select>
      </div>
      <div id="sc-list" style="margin-top:12px"><div class="empty-tip">加载中…</div></div>
    </div>`;
  const load = async (status) => {
    try {
      const rows = await api(`/api/sync/conflicts?status=${status}`);
      $("#sc-list").innerHTML = rows.length ? rows.map((c) => `
        <div class="panel panel-pad" style="box-shadow:none;margin-bottom:10px" data-cid="${c.id}">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
            <div><b>${esc(c.entity_label)}：${esc(c.name)}</b>
              <span class="app-desc mono">${esc(c.external_key)}</span>
              ${c.status === "pending"
                ? '<span class="badge maintenance">待裁决</span>'
                : `<span class="badge ${c.status === "take_upstream" ? "developing" : "online"}">`
                  + (c.status === "take_upstream" ? "已取上游" : "已留本地") + "</span>"}
            </div>
            <div>
              ${c.status === "pending"
                ? `<button class="btn small primary" data-act="decide">查看两边差异并裁决</button>`
                : `<button class="btn small" data-act="view">查看裁决记录</button>`}
            </div>
          </div>
          <div class="app-desc" style="margin-top:4px">
            撞车字段：${esc((c.entries || []).filter((e) => e.state === "conflict").map((e) => e.label).join("、") || "无")}
          </div>
        </div>`).join("")
        : '<div class="empty-tip">没有冲突记录。可在同步台把模拟源切到「改动撞车」后跑一趟制造样例。</div>';
      $$("[data-cid]", $("#sc-list")).forEach((el) => {
        el.onclick = () => openConflictModal(parseInt(el.dataset.cid, 10), () => load($("#sc-filter").value));
      });
    } catch (e) {
      $("#sc-list").innerHTML = errorStateHtml("加载失败", e.message);
    }
  };
  $("#sc-filter").onchange = () => load($("#sc-filter").value);
  load("pending");
}

const CONFLICT_STATE_TXT = {
  same: "三方一致", upstream_only: "仅上游改动", local_only: "仅本地改动",
  both_same: "两边改成同值", conflict: "两边改成不同值",
};

async function openConflictModal(cid, onClose) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-mask"><div class="modal" style="width:860px">
    <h3>冲突三方对比 #${cid}</h3><div id="cm-body"><div class="empty-tip">加载中…</div></div>
  </div></div>`;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) { root.innerHTML = ""; if (onClose) onClose(); } };
  let c;
  try { c = await api(`/api/sync/conflicts/${cid}`); }
  catch (e) { $("#cm-body").innerHTML = errorStateHtml("加载失败", e.message); return; }

  const rowCls = (st) => st === "conflict" ? "background:#fde8e8"
    : st === "upstream_only" ? "background:#e8f0ff"
    : st === "local_only" ? "background:#e5f8ee"
    : st === "both_same" ? "background:#fdf3d7" : "";
  const decided = c.status !== "pending";
  $("#cm-body").innerHTML = `
    <p style="margin:0 0 10px">${esc(c.entity_label)} <b>${esc(c.name)}</b>
      <span class="mono app-desc">${esc(c.external_key)}</span>：本地这段时间有人改过，上游这一趟也推了新值。
      后到的不会盖掉先到的，请逐字段看清差异后选择整单留哪边。</p>
    <div class="table-wrap"><table class="env-table" style="white-space:normal">
      <thead><tr><th>字段</th><th>共同基线（上次同步）</th><th style="color:#0f8a4c">本地现值</th>
      <th style="color:#2f6bff">上游新值</th><th>判定</th></tr></thead>
      <tbody>${(c.entries || []).map((e) => `
        <tr style="${rowCls(e.state)}">
          <td><b>${esc(e.label)}</b></td>
          <td>${esc(e.common || "（空）")}</td>
          <td>${esc(e.local || "（空）")}</td>
          <td>${esc(e.upstream || "（空）")}</td>
          <td>${esc(CONFLICT_STATE_TXT[e.state] || e.state)}</td>
        </tr>`).join("")}
      </tbody></table></div>
    ${decided ? `
      <div class="panel panel-pad" style="box-shadow:none;margin-top:14px;background:#fafbfc">
        <b>裁决结果：</b>${c.status === "take_upstream" ? "采用上游值" : "保留本地值"}<br>
        裁决人：${esc(c.decided_by_name || "—")} · 时间：${c.decided_at ? fmtTime(c.decided_at) : "—"}<br>
        备注：${esc(c.decide_note || "（无）")}
      </div>
      <div style="text-align:right;margin-top:14px"><button class="btn" id="cm-close">关闭</button></div>` : `
      <div style="margin-top:14px">
        <label class="k">裁决备注（可选，会记入留痕）</label>
        <input id="cm-note" class="" maxlength="200" style="width:100%;margin-top:4px;padding:7px 10px;border:1px solid var(--line);border-radius:6px" placeholder="例如：已与上游负责人确认，以上游口径为准">
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:14px;gap:8px">
        <button class="btn" id="cm-cancel">取消</button>
        <div style="display:flex;gap:8px">
          <button class="btn" id="cm-local" style="border-color:#0f8a4c;color:#0f8a4c">保留本地值</button>
          <button class="btn primary" id="cm-up">采用上游值</button>
        </div>
      </div>`}`;

  if (decided) {
    $("#cm-close").onclick = () => { root.innerHTML = ""; if (onClose) onClose(); };
    return;
  }
  const decide = async (choice) => {
    try {
      const r = await api(`/api/sync/conflicts/${cid}/decide`, {
        method: "POST", body: { choice, note: $("#cm-note").value.trim() },
      });
      toast("裁决已记录：" + r.detail, "success");
      root.innerHTML = "";
      renderSync();
    } catch (e) { toast(e.message, "error"); }
  };
  $("#cm-cancel").onclick = () => { root.innerHTML = ""; };
  $("#cm-local").onclick = () => decide("keep_local");
  $("#cm-up").onclick = () => decide("take_upstream");
}

/* ---------- 上游删除处理 ---------- */
async function paintSyncDeletions() {
  const body = $("#sync-body");
  body.innerHTML = `
    <div class="panel panel-pad">
      <p style="margin:0 0 10px;color:var(--ink-2)">上游报文里消失（上游已删）、但本地仍挂着的记录都列在这里：
        不会假装没看见继续挂着，需明确选择 <b>下线本地实体</b> 或 <b>确认忽略</b>，处理人与处理结果记入留痕。</p>
      <div id="sd-list"><div class="empty-tip">加载中…</div></div>
    </div>`;
  try {
    const rows = await api("/api/sync/remote-deletions?status=all");
    $("#sd-list").innerHTML = rows.length ? `
      <div class="table-wrap"><table class="app-table">
        <thead><tr><th>类型</th><th>名称 / 上游标识</th><th>状态</th><th>首次发现</th>
        <th>处理人 / 备注</th><th></th></tr></thead>
        <tbody>${rows.map((d) => `
          <tr>
            <td>${esc(d.entity_label)}</td>
            <td><b>${esc(d.name)}</b><div class="app-desc mono">${esc(d.external_key)}</div></td>
            <td>${d.status === "pending" ? '<span class="badge maintenance">待处理</span>'
              : d.status === "offlined" ? '<span class="badge offline">已下线</span>'
              : '<span class="badge developing">已忽略</span>'}</td>
            <td>#${d.first_seen_run} 趟<br><span class="app-desc">${fmtTime(d.first_seen_at)}</span></td>
            <td style="white-space:normal;max-width:260px">${d.handled_by_name ? `${esc(d.handled_by_name)}：${esc(d.handle_note || "（无备注）")}` : "—"}</td>
            <td style="white-space:nowrap">${d.status === "pending" ? `
              <button class="btn small danger" data-id="${d.id}" data-act="offline">下线本地实体</button>
              <button class="btn small" data-id="${d.id}" data-act="ignore">确认忽略</button>` : ""}</td>
          </tr>`).join("")}
        </tbody></table></div>`
      : '<div class="empty-tip">没有上游删除记录。同步过程中发现报文里消失的本地实体会自动登记到这里。</div>';
    $$("#sd-list [data-act]").forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        const id = parseInt(b.dataset.id, 10);
        const act = b.dataset.act;
        openNoteModal({
          title: act === "offline" ? "下线本地实体" : "确认忽略上游删除",
          message: act === "offline"
            ? "将本地实体置为下线（应用置「下线」终态；模块标记已下线；无挂载的环境直接删除），并留痕。继续？"
            : "保留本地实体并不再提醒该条上游删除（该决定会留痕）。继续？",
          confirmText: act === "offline" ? "确认下线" : "确认忽略",
          danger: act === "offline",
          onConfirm: async (note) => {
            try {
              const r = await api(`/api/sync/remote-deletions/${id}/handle`, {
                method: "POST", body: { action: act, note },
              });
              toast(r.detail, "success");
              paintSyncDeletions();
            } catch (err) { toast(err.message, "error"); throw err; }
          },
        });
      };
    });
  } catch (e) {
    $("#sd-list").innerHTML = errorStateHtml("加载失败", e.message);
  }
}

/* ---------- 本地删除（墓碑） ---------- */
async function paintSyncTombstones() {
  const body = $("#sync-body");
  body.innerHTML = `
    <div class="panel panel-pad" style="margin-bottom:14px">
      <h3>本地删除记录（拒收复活）</h3>
      <p style="margin:0 0 10px;color:var(--ink-2)">本地删除同步来源实体时会立墓碑：之后上游再推同标识记录，
        只记一条"拒收复活"，<b>不会偷偷复活成一条新记录</b>。</p>
      <div id="st-list"><div class="empty-tip">加载中…</div></div>
    </div>
    <div class="panel panel-pad">
      <h3>当前本地同步实体（可本地删除以演示拒收）</h3>
      <div id="sl-list"><div class="empty-tip">加载中…</div></div>
    </div>`;
  const [tombs, local] = await Promise.all([
    api("/api/sync/tombstones"), api("/api/sync/local-entities"),
  ]);
  $("#st-list").innerHTML = tombs.length ? `
    <div class="table-wrap"><table class="app-table">
      <thead><tr><th>类型</th><th>名称</th><th>上游标识</th><th>删除人</th><th>本地删除时间</th><th>上游最近仍推送</th></tr></thead>
      <tbody>${tombs.map((t) => `
        <tr><td>${esc(t.entity_label)}</td><td><b>${esc(t.name)}</b></td>
        <td class="mono">${esc(t.external_key)}</td>
        <td>${esc(t.deleted_by_name)}</td><td>${fmtTime(t.deleted_at)}</td>
        <td>${t.last_pushed_at ? `第 ${t.last_pushed_run || "?"} 趟已拒收` : "删除后上游暂未再推"}</td>
        </tr>`).join("")}</tbody></table></div>`
    : '<div class="empty-tip">暂无本地删除记录</div>';

  const sec = (title, rows, cols, etype) => rows.length ? `
    <p style="margin:14px 0 6px"><b>${title}（${rows.length}）</b></p>
    <div class="table-wrap"><table class="env-table">
      <thead><tr>${cols.map((c) => `<th>${c[1]}</th>`).join("")}<th></th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr>${cols.map((c) => `<td>${esc(c[2](r))}</td>`).join("")}
        <td style="white-space:nowrap"><button class="btn small danger" data-del="${etype}" data-id="${r.id}">本地删除（拒收后续推送）</button></td>
        </tr>`).join("")}</tbody></table></div>` : "";
  $("#sl-list").innerHTML = [
    sec("同步应用", local.apps, [
      ["id", "ID", (r) => r.id], ["name", "应用", (r) => r.name],
      ["bl", "业务线", (r) => r.business_line_name],
      ["ext", "上游标识", (r) => r.external_key],
    ], "app"),
    sec("同步模块", local.modules, [
      ["id", "ID", (r) => r.id], ["name", "模块", (r) => r.name],
      ["app", "所属应用", (r) => r.app_name], ["ext", "上游标识", (r) => r.external_key],
    ], "module"),
    sec("同步环境", local.environments, [
      ["id", "ID", (r) => r.id], ["name", "环境", (r) => `${r.env_label}（${r.env_key}）`],
      ["app", "所属应用", (r) => r.app_name], ["ext", "上游标识", (r) => r.external_key],
    ], "environment"),
  ].join("") || '<div class="empty-tip">暂无同步来源实体，先跑一趟同步</div>';
  $$("[data-del]", $("#sl-list")).forEach((b) => {
    b.onclick = () => {
      const etype = b.dataset.del;
      const id = parseInt(b.dataset.id, 10);
      const label = { app: "应用", module: "模块", environment: "环境" }[etype];
      openConfirmModal({
        title: `本地删除同步${label}`,
        message: `删除后将立墓碑：上游再推送同标识的${label}时只记拒收，不会复活为新记录。确定删除？`,
        confirmText: "本地删除", danger: true,
        onConfirm: async () => {
          try {
            await api("/api/sync/local-entities/delete", {
              method: "POST", body: { entity_type: etype, local_id: id },
            });
            toast("已本地删除并立墓碑", "success");
            renderSync();
          } catch (e) { toast(e.message, "error"); throw e; }
        },
      });
    };
  });
}

/* ---------- 同步留痕 ---------- */
async function paintSyncAudit() {
  const body = $("#sync-body");
  body.innerHTML = `<div class="panel panel-pad"><div id="sa-list"><div class="empty-tip">加载中…</div></div></div>`;
  try {
    const rows = await api("/api/sync/audit?limit=200");
    $("#sa-list").innerHTML = rows.length ? `
      <div class="table-wrap"><table class="app-table">
        <thead><tr><th>时间</th><th>动作</th><th>操作人</th><th>对象</th><th>详情</th><th>趟次</th></tr></thead>
        <tbody>${rows.map((l) => `
          <tr>
            <td style="white-space:nowrap">${fmtTime(l.created_at)}</td>
            <td>${esc(l.action_label)}</td>
            <td>${esc(l.actor_name)}</td>
            <td>${l.entity_label ? `${esc(l.entity_label)}<div class="app-desc mono">${esc(l.external_key)}</div>` : "—"}</td>
            <td style="white-space:normal;max-width:520px">${esc(l.detail)}</td>
            <td>${l.run_id ? `#${l.run_id}` : "—"}</td>
          </tr>`).join("")}
        </tbody></table></div>`
      : '<div class="empty-tip">暂无同步留痕</div>';
  } catch (e) {
    $("#sa-list").innerHTML = errorStateHtml("加载失败", e.message);
  }
}

/* ---------- 通用确认 / 备注弹窗 ---------- */
function _modalShell(width) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-mask"><div class="modal" style="width:${width || 440}px">
    <h3 id="gm-title"></h3><div id="gm-body"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
      <button class="btn" id="gm-cancel">取消</button>
      <button class="btn primary" id="gm-ok"></button>
    </div></div></div>`;
  const close = () => { root.innerHTML = ""; };
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  $("#gm-cancel").onclick = close;
  return { root, close };
}

function openConfirmModal({ title, message, confirmText, danger, onConfirm }) {
  const { close } = _modalShell();
  $("#gm-title").textContent = title;
  $("#gm-body").innerHTML = `<p style="margin:0;color:var(--ink-2)">${esc(message)}</p>`;
  const ok = $("#gm-ok");
  ok.textContent = confirmText || "确认";
  if (danger) ok.classList.add("danger");
  ok.onclick = async () => {
    ok.disabled = true;
    try {
      await onConfirm();
      close();
    } catch (e) { ok.disabled = false; }
  };
}

function openNoteModal({ title, message, confirmText, danger, onConfirm }) {
  const { close } = _modalShell(480);
  $("#gm-title").textContent = title;
  $("#gm-body").innerHTML = `
    <p style="margin:0 0 10px;color:var(--ink-2)">${esc(message)}</p>
    <input id="gm-note" maxlength="200" style="width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px" placeholder="处理备注（可选，记入留痕）">`;
  const ok = $("#gm-ok");
  ok.textContent = confirmText || "确认";
  if (danger) ok.classList.add("danger");
  ok.onclick = async () => {
    ok.disabled = true;
    try {
      await onConfirm($("#gm-note").value.trim());
      close();
    } catch (e) { ok.disabled = false; }
  };
}

/* ---------------- 错误态 ---------------- */
function errorStateHtml(title, message) {
  return `
    <div class="panel error-state">
      <div class="code">${esc(title)}</div>
      <div class="msg">${esc(message)}</div>
      <button class="btn" onclick="location.hash='#/apps'">返回应用台账</button>
      <button class="btn" onclick="location.hash='#/console'" style="margin-left:8px">回到控制台</button>
    </div>`;
}

/* ---------------- 入口 ---------------- */
(async function init() {
  if (state.token) {
    try {
      state.user = await api("/api/me");
      await bootstrap();
      return;
    } catch (e) { /* token 失效，走登录 */ }
  }
  showLogin();
})();
