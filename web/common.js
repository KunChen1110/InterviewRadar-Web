/* InterviewRadar 共享 JS：API 封装 / 鉴权 / 导航栏 / toast */
const IR = (() => {
  const TOKEN_KEY = "ir_token";

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }
  function setToken(t) {
    try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch (e) {}
  }

  // 页面位于 /web/ 下时 API 走 ../api/；落地页可能被直接服务在根路径 /，此时走 api/
  const IN_WEB = location.pathname.includes("/web/");
  const API_BASE = IN_WEB ? "../api/" : "api/";
  const PAGE_BASE = IN_WEB ? "" : "web/";

  function pageHref(h) { return PAGE_BASE + h; }

  // 根路径上下文下，把页面内的相对 .html 链接统一补 web/ 前缀
  if (!IN_WEB) {
    document.querySelectorAll("a[href]").forEach(a => {
      const h = a.getAttribute("href") || "";
      if (h && !h.startsWith("http") && !h.startsWith("#") && !h.startsWith("web/") && h.includes(".html")) {
        a.setAttribute("href", "web/" + h);
      }
    });
  }

  async function api(path, method, body) {
    const opts = { method: method || "GET", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const t = getToken();
    if (t) opts.headers["Authorization"] = "Bearer " + t;
    const resp = await fetch(API_BASE + path, opts);
    let data = {};
    try { data = await resp.json(); } catch (e) {}
    if (!resp.ok) {
      if (resp.status === 401) {
        setToken("");
        const here = location.pathname.split("/").pop() + location.search;
        location.href = pageHref("auth.html?next=") + encodeURIComponent(here);
        throw new Error("请先登录");
      }
      throw new Error(data.detail || ("请求失败 " + resp.status));
    }
    return data;
  }

  function requireAuth() {
    if (!getToken()) {
      const here = location.pathname.split("/").pop() + location.search;
      location.href = pageHref("auth.html?next=") + encodeURIComponent(here);
      return false;
    }
    return true;
  }

  function toast(msg, isErr) {
    document.querySelectorAll(".toast").forEach(el => el.remove());
    const div = document.createElement("div");
    div.className = "toast" + (isErr ? " err" : "");
    div.textContent = msg;
    document.body.appendChild(div);
    setTimeout(() => div.remove(), 2600);
  }

  const NAV = [
    ["dashboard.html", "控制台"],
    ["community.html", "面经社区"],
    ["submit.html", "提交面经"],
    ["settings.html", "账户设置"],
  ];

  async function renderTopbar(active) {
    const host = document.getElementById("topbar");
    if (!host) return;
    host.innerHTML =
      '<header class="nav"><div class="nav-inner">' +
      '<a class="nav-brand" href="' + pageHref("landing.html") + '">🤖 InterviewRadar</a>' +
      '<nav class="nav-links">' +
      NAV.map(([href, name]) =>
        '<a href="' + pageHref(href) + '"' + (href === active ? ' class="active"' : "") + ">" + name + "</a>"
      ).join("") +
      "</nav>" +
      '<div class="nav-user" id="navUser"></div>' +
      "</div></header>";
    const userBox = document.getElementById("navUser");
    if (!getToken()) {
      userBox.innerHTML =
        '<a class="btn sm secondary" href="' + pageHref("auth.html") + '">登录</a>' +
        '<a class="btn sm primary" href="' + pageHref("auth.html?mode=register") + '">免费注册</a>';
      return;
    }
    try {
      const d = await api("auth/me");
      const u = d.user;
      userBox.innerHTML =
        '<span class="nick">' + escapeHtml(u.nickname || u.email) + "</span>" +
        '<a class="logout" href="#" id="logoutBtn">退出</a>';
      document.getElementById("logoutBtn").addEventListener("click", e => {
        e.preventDefault();
        logout();
      });
    } catch (e) { /* 401 时 api() 已跳转 */ }
  }

  async function logout() {
    try { await api("auth/logout", "POST", {}); } catch (e) {}
    setToken("");
    location.href = pageHref("landing.html");
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function fmtDate(s) {
    if (!s) return "";
    return String(s).slice(0, 10);
  }

  return { api, getToken, setToken, requireAuth, toast, renderTopbar, logout, escapeHtml, fmtDate, pageHref };
})();
