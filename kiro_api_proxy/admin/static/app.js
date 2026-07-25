"use strict";

// 管理界面前端逻辑：登录门禁 + 标签页 + 各功能面板。
// API Key 存于 sessionStorage，随请求以 x-api-key 头发送。

const API_BASE = "/admin/api";
const KEY_STORAGE = "kiro_admin_key";

let apiKey = sessionStorage.getItem(KEY_STORAGE) || "";

function $(id) {
  return document.getElementById(id);
}

function setText(id, text, cls) {
  const el = $(id);
  if (!el) return;
  el.textContent = text || "";
  if (cls) el.className = cls;
}

// 统一请求封装，自动带鉴权头并解析错误。
async function api(path, options = {}) {
  const headers = Object.assign(
    { "Content-Type": "application/json" },
    options.headers || {}
  );
  if (apiKey) headers["x-api-key"] = apiKey;
  const resp = await fetch(API_BASE + path, { ...options, headers });
  let data = null;
  try {
    data = await resp.json();
  } catch (_) {
    data = null;
  }
  if (!resp.ok) {
    const detail =
      (data && (data.detail || (data.error && data.error.message))) ||
      `HTTP ${resp.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

// ---- 登录门禁 ----

async function checkAuth() {
  const status = await api("/status");
  if (status.requires_auth && !status.authenticated) {
    $("loginGate").classList.remove("hidden");
    $("panels").classList.add("hidden");
    setText("authStatus", "未登录", "auth-status err");
    return false;
  }
  $("loginGate").classList.add("hidden");
  $("panels").classList.remove("hidden");
  setText("authStatus", status.requires_auth ? "已登录" : "免鉴权", "auth-status ok");
  // 未设置 API Key（无鉴权）时展示提醒条，引导前往设置页生成。
  $("noKeyNotice").classList.toggle("hidden", Boolean(status.requires_auth));
  renderCredInfo(status.credentials);
  return true;
}

async function login() {
  const key = $("loginKey").value.trim();
  apiKey = key;
  sessionStorage.setItem(KEY_STORAGE, key);
  setText("loginError", "");
  try {
    const ok = await checkAuth();
    if (!ok) setText("loginError", "API Key 无效");
  } catch (err) {
    setText("loginError", err.message);
  }
}

// ---- 概览：登录配置 ----

function renderCredInfo(cred) {
  const dl = $("credInfo");
  if (!dl) return;
  if (!cred || !cred.configured) {
    dl.innerHTML =
      '<dt>状态</dt><dd>尚未配置凭据，请前往“登录授权”登录或导入。</dd>';
    return;
  }
  const rows = [];
  const add = (k, v) => {
    if (v !== undefined && v !== null && v !== "")
      rows.push(`<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`);
  };
  add("凭据文件", cred.path);
  if (cred.error) {
    add("错误", cred.error);
  } else {
    add("Profile ARN", cred.profile_arn);
    add("登录区域", cred.auth_region);
    add("数据面区域", cred.endpoint_region);
    if (cred.source_index !== null && cred.source_index !== undefined)
      add("账户索引", cred.source_index);
    if (cred.expires_at)
      add("Token 到期", new Date(cred.expires_at * 1000).toLocaleString());
  }
  dl.innerHTML = rows.join("");
}

async function refreshStatus() {
  try {
    const status = await api("/status");
    renderCredInfo(status.credentials);
  } catch (err) {
    alert(err.message);
  }
}

// ---- 额度 ----

async function refreshUsage() {
  const body = $("usageBody");
  body.innerHTML = '<p class="muted">查询中…</p>';
  try {
    const u = await api("/usage");
    body.innerHTML = renderUsage(u);
  } catch (err) {
    body.innerHTML = `<p class="error">${escapeHtml(err.message)}</p>`;
  }
}

function renderUsage(u) {
  const pct = Math.min(100, Math.round((u.usage_percent || 0) * 100));
  const rows = [];
  const add = (k, v) => {
    if (v !== undefined && v !== null && v !== "")
      rows.push(`<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`);
  };
  add("邮箱", u.email);
  add("订阅", u.subscription_title || u.subscription_type);
  add("资源类型", u.resource_type);
  add("下次重置", u.next_reset_date);
  if (u.trial_status) add("试用状态", u.trial_status);
  if (u.trial_usage_limit > 0)
    add("试用配额", `${u.trial_usage_current} / ${u.trial_usage_limit}`);
  const limitText =
    u.usage_limit > 0
      ? `${u.usage_current} / ${u.usage_limit}`
      : String(u.usage_current || 0);
  return `
    <div class="usage-bar"><div class="usage-fill" style="width:${pct}%"></div></div>
    <p class="usage-pct">已用 ${limitText}（${pct}%）</p>
    <dl class="kv">${rows.join("")}</dl>
  `;
}

// ---- SSO 登录 ----

let ssoSessionId = "";
let ssoPollTimer = null;

function stopSsoPoll() {
  if (ssoPollTimer !== null) {
    clearTimeout(ssoPollTimer);
    ssoPollTimer = null;
  }
}

async function ssoStart() {
  setText("ssoError", "");
  setText("ssoOk", "");
  stopSsoPoll();
  const startUrl = $("ssoStartUrl").value.trim();
  const region = $("ssoRegion").value.trim() || "us-east-1";
  // 记住本次输入，下次打开无需重新填写。
  try {
    localStorage.setItem("ssoStartUrl", startUrl);
    localStorage.setItem("ssoRegion", region);
  } catch (_) {}
  try {
    const res = await api("/sso/start", {
      method: "POST",
      body: JSON.stringify({
        start_url: startUrl,
        region: region,
      }),
    });
    ssoSessionId = res.session_id;
    const link = $("ssoLink");
    link.href = res.authorize_url;
    link.textContent = res.authorize_url;
    $("ssoStep").classList.remove("hidden");
    // auto=true：回调直达本服务，轮询自动完成；否则回退手动粘贴回调 URL。
    const auto = Boolean(res.auto);
    $("ssoAuto").classList.toggle("hidden", !auto);
    $("ssoManual").classList.toggle("hidden", auto);
    if (auto) {
      // 会话 TTL 600s，轮询至完成/失败/过期为止。
      pollSso(ssoSessionId, Date.now() + 600000);
    }
  } catch (err) {
    setText("ssoError", err.message);
  }
}

// 轮询自动回调进度：pending 继续等待，终态更新界面并停止。
function pollSso(sessionId, deadline) {
  ssoPollTimer = setTimeout(async () => {
    // 期间用户又发起了新会话，丢弃本轮结果。
    if (sessionId !== ssoSessionId) return;
    try {
      const res = await api(
        `/sso/poll?session_id=${encodeURIComponent(sessionId)}`
      );
      if (sessionId !== ssoSessionId) return;
      if (res.status === "success") {
        stopSsoPoll();
        setText("ssoOk", `登录成功，凭据已保存到 ${res.path}`, "ok");
        $("ssoStep").classList.add("hidden");
        refreshStatus();
        return;
      }
      if (res.status === "error") {
        stopSsoPoll();
        setText("ssoError", res.error || "登录失败，请重试");
        return;
      }
      if (res.status === "not_found") {
        stopSsoPoll();
        setText("ssoError", "登录会话已过期，请重新发起登录");
        return;
      }
    } catch (_) {
      // 单次轮询网络抖动不终止，继续等待。
    }
    if (Date.now() > deadline) {
      stopSsoPoll();
      setText("ssoError", "登录超时，请重新发起登录");
      return;
    }
    pollSso(sessionId, deadline);
  }, 2000);
}

async function ssoComplete() {
  setText("ssoError", "");
  setText("ssoOk", "");
  try {
    const res = await api("/sso/complete", {
      method: "POST",
      body: JSON.stringify({
        session_id: ssoSessionId,
        callback_url: $("ssoCallback").value.trim(),
      }),
    });
    setText("ssoOk", `登录成功，凭据已保存到 ${res.path}`, "ok");
    $("ssoStep").classList.add("hidden");
    refreshStatus();
  } catch (err) {
    setText("ssoError", err.message);
  }
}

// ---- 从本机 Kiro 登录导入 ----

async function scanLocal() {
  setText("localError", "");
  setText("localOk", "");
  const list = $("localList");
  list.innerHTML = '<p class="muted">扫描中…</p>';
  try {
    const res = await api("/credentials/scan");
    renderLocalList(res.credentials || []);
  } catch (err) {
    list.innerHTML = "";
    setText("localError", err.message);
  }
}

function renderLocalList(creds) {
  const list = $("localList");
  if (!creds.length) {
    list.innerHTML =
      '<p class="muted">未发现本机 Kiro 登录凭据（未检测到 AWS SSO 缓存）。</p>';
    return;
  }
  list.innerHTML = creds
    .map((c) => {
      const meta = [c.auth_method, c.provider, c.region]
        .filter(Boolean)
        .map(escapeHtml)
        .join(" · ");
      const warn = c.has_client_secret
        ? ""
        : '<span class="muted">（缺 client 信息，可能无法刷新）</span>';
      return `
        <div class="local-item">
          <div class="local-meta">
            <div>${meta || "Kiro 凭据"} ${warn}</div>
            <div class="muted">${escapeHtml(c.token_file)}</div>
            ${c.expires_at ? `<div class="muted">Token 到期：${escapeHtml(c.expires_at)}</div>` : ""}
          </div>
          <button data-local-id="${escapeHtml(c.id)}">导入</button>
        </div>`;
    })
    .join("");
  list.querySelectorAll("button[data-local-id]").forEach((btn) => {
    btn.addEventListener("click", () => importLocal(btn.dataset.localId, btn));
  });
}

async function importLocal(id, btn) {
  setText("localError", "");
  setText("localOk", "");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "导入中…";
  }
  try {
    const res = await api("/credentials/import-local", {
      method: "POST",
      body: JSON.stringify({ id }),
    });
    setText("localOk", `导入成功，凭据已保存到 ${res.path}`, "ok");
    refreshStatus();
  } catch (err) {
    setText("localError", err.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "导入";
    }
  }
}

// ---- 凭据导入 ----

// 选中文件后读取文本填入文本框，随后走与粘贴一致的导入流程。
function handleCredFile(e) {
  setText("credError", "");
  setText("credOk", "");
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    $("credJson").value = String(reader.result || "");
    setText("credOk", `已载入文件 ${file.name}，点击“导入并保存”确认`, "ok");
  };
  reader.onerror = () => setText("credError", "读取文件失败");
  reader.readAsText(file);
}

async function importCredentials() {
  setText("credError", "");
  setText("credOk", "");
  const content = $("credJson").value.trim();
  if (!content) {
    setText("credError", "请粘贴凭据 JSON");
    return;
  }
  const indexRaw = $("credIndex").value.trim();
  try {
    const res = await api("/credentials/import", {
      method: "POST",
      body: JSON.stringify({
        content,
        account_index: indexRaw === "" ? null : Number(indexRaw),
      }),
    });
    setText("credOk", `导入成功，已保存到 ${res.path}`, "ok");
    refreshStatus();
  } catch (err) {
    setText("credError", err.message);
  }
}

// ---- 设置 ----

async function loadSettings() {
  try {
    const s = await api("/settings");
    $("setHost").value = s.api_host || "";
    $("setPort").value = s.api_port || "";
    // 回显真实 api_key（本端点受鉴权保护）；默认隐藏，点小眼睛查看。
    const keyInput = $("setApiKey");
    keyInput.value = s.api_key || "";
    keyInput.type = "password";
    $("toggleApiKey").textContent = "显示";
  } catch (err) {
    setText("setError", err.message);
  }
}

// 小眼睛：切换 API Key 明文/密文显示。
function toggleApiKeyVisibility() {
  const input = $("setApiKey");
  const shown = input.type !== "password";
  input.type = shown ? "password" : "text";
  $("toggleApiKey").textContent = shown ? "显示" : "隐藏";
}

// 客户端生成随机 API Key（等价 secrets.token_urlsafe(32)：32 字节 base64url）。
function generateApiKey() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  const key = btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const input = $("setApiKey");
  input.value = key;
  input.type = "text";
  $("toggleApiKey").textContent = "隐藏";
  setText("setOk", "已生成，点击“保存设置”生效", "ok");
}

async function saveSettings() {
  setText("setError", "");
  setText("setOk", "");
  const newKey = $("setApiKey").value.trim();
  const patch = {
    api_host: $("setHost").value.trim(),
    api_port: Number($("setPort").value),
    // 总是提交 api_key：留空即清除（恢复默认，通常为无鉴权），非空则设置。
    api_key: newKey || null,
  };
  try {
    const res = await api("/settings", {
      method: "POST",
      body: JSON.stringify(patch),
    });
    let msg = "已保存";
    if (res.restart_required) msg += "，监听地址/端口将在重启后生效";
    setText("setOk", msg, "ok");
    // 同步本地会话密钥，避免后续请求 401（清空时一并清除）。
    apiKey = newKey;
    if (newKey) sessionStorage.setItem(KEY_STORAGE, newKey);
    else sessionStorage.removeItem(KEY_STORAGE);
    // 刷新鉴权状态：更新顶部标识与无鉴权提醒条。
    await checkAuth();
  } catch (err) {
    setText("setError", err.message);
  }
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => {
    return {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c];
  });
}

// ---- 标签切换 ----

function setupTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document
        .querySelectorAll(".tabpane")
        .forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      const name = tab.dataset.tab;
      document.querySelector(`.tabpane[data-pane="${name}"]`).classList.add("active");
      if (name === "settings") loadSettings();
    });
  });
}

// ---- 初始化 ----

function init() {
  setupTabs();
  $("loginBtn").addEventListener("click", login);
  $("loginKey").addEventListener("keydown", (e) => {
    if (e.key === "Enter") login();
  });
  $("refreshStatus").addEventListener("click", refreshStatus);
  $("refreshUsage").addEventListener("click", refreshUsage);
  $("scanLocalBtn").addEventListener("click", scanLocal);
  $("ssoStartBtn").addEventListener("click", ssoStart);
  $("ssoCompleteBtn").addEventListener("click", ssoComplete);
  // 回填上一次的 Start URL / 区域，避免每次重新输入。
  try {
    const savedUrl = localStorage.getItem("ssoStartUrl");
    const savedRegion = localStorage.getItem("ssoRegion");
    if (savedUrl) $("ssoStartUrl").value = savedUrl;
    if (savedRegion) $("ssoRegion").value = savedRegion;
  } catch (_) {}
  $("credFile").addEventListener("change", handleCredFile);
  $("credImportBtn").addEventListener("click", importCredentials);
  $("saveSettings").addEventListener("click", saveSettings);
  $("toggleApiKey").addEventListener("click", toggleApiKeyVisibility);
  $("genApiKey").addEventListener("click", generateApiKey);
  // 提醒条里的「设置」链接：切到设置页。
  $("noKeyGoSettings").addEventListener("click", (e) => {
    e.preventDefault();
    document.querySelector('.tab[data-tab="settings"]').click();
  });

  checkAuth().catch((err) => {
    setText("loginError", err.message);
    $("loginGate").classList.remove("hidden");
  });
}

document.addEventListener("DOMContentLoaded", init);
