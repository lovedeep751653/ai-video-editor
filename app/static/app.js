/* AI editor open source — phone screen.
   Talks to the editor engine only through the routes in docs/API.md. */
"use strict";

const APP = typeof window.Android !== "undefined" && window.Android !== null;
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const SVGNS = "http://www.w3.org/2000/svg";

/* ------------------------------------------------------------------ */
/* Small helpers                                                       */
/* ------------------------------------------------------------------ */

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "style") el.style.cssText = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function icon(name, cls = "") {
  const s = document.createElementNS(SVGNS, "svg");
  s.setAttribute("class", "ic " + cls);
  s.setAttribute("aria-hidden", "true");
  const u = document.createElementNS(SVGNS, "use");
  u.setAttribute("href", "#i-" + name);
  s.append(u);
  return s;
}

function img(src, alt = "") {
  const i = h("img", { alt, loading: "lazy", decoding: "async" });
  i.addEventListener("load", () => i.classList.add("loaded"));
  i.addEventListener("error", () => i.classList.add("broken"));
  if (src) i.src = src;
  return i;
}

const pad = (n) => String(n).padStart(2, "0");

function fmtDur(s) {
  if (s == null || !isFinite(s)) return "";
  s = Math.max(0, Math.round(s));
  const hr = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return hr ? `${hr}:${pad(m)}:${pad(x)}` : `${m}:${pad(x)}`;
}

function fmtSize(b) {
  if (!b && b !== 0) return "";
  if (b < 1024) return `${b} B`;
  const u = ["KB", "MB", "GB"];
  let i = -1;
  do { b /= 1024; i++; } while (b >= 1024 && i < u.length - 1);
  return `${b >= 100 ? Math.round(b) : b.toFixed(1)} ${u[i]}`;
}

function fmtEta(s) {
  if (s == null || !isFinite(s)) return "";
  if (s < 10) return "Almost done";
  if (s < 60) return `About ${Math.max(10, Math.round(s / 10) * 10)} sec left`;
  const m = Math.round(s / 60);
  if (m < 60) return `About ${m} min left`;
  const hr = Math.floor(m / 60), mm = m % 60;
  return `About ${hr} hr ${mm ? mm + " min " : ""}left`;
}

function toDate(t) {
  if (t == null) return null;
  if (typeof t === "number") return new Date(t < 1e12 ? t * 1000 : t);
  const d = new Date(t);
  return isNaN(d) ? null : d;
}

function fmtAgo(t) {
  const d = toDate(t);
  if (!d) return "";
  const sec = (Date.now() - d.getTime()) / 1000;
  if (sec < 60) return "Just now";
  if (sec < 3600) return `${Math.floor(sec / 60)} min ago`;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  if (d >= today) return `Today ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  if (d >= new Date(today.getTime() - 86400000)) return "Yesterday";
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: d.getFullYear() === today.getFullYear() ? undefined : "numeric" });
}

const FORMAT_NAMES = { "9:16": "Tall", "16:9": "Wide", "1:1": "Square", original: "Original shape" };
const MODE_NAMES = { highlight: "Highlight reel", cleanup: "Cleaned-up video" };
const safeName = (s) => (String(s || "video").replace(/[\\/:*?"<>|]+/g, " ").replace(/\s+/g, " ").trim() || "video").slice(0, 60);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch (_) { /* storage blocked: fine */ }
  return null;
}

/* ------------------------------------------------------------------ */
/* Talking to the editor                                               */
/* ------------------------------------------------------------------ */

const NET_ERROR = "Could not reach the editor. Please check the app is open and try again.";

async function api(path, { method = "GET", json, form } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (json !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(json); }
  else if (form) opts.body = form;
  let res;
  try { res = await fetch(path, opts); } catch (_) { throw Object.assign(new Error(NET_ERROR), { network: true }); }
  let data = {};
  try { data = await res.json(); } catch (_) { /* not JSON */ }
  if (res.status === 401 && data && data.login) {
    await needLogin();
    return api(path, { method, json, form });
  }
  if (!res.ok) {
    const e = new Error((data && data.detail && String(data.detail)) || `Something went wrong (error ${res.status}). Please try again.`);
    e.status = res.status;
    throw e;
  }
  return data;
}

/* Upload with real progress (browser mode). */
function uploadFiles(fileList, onProgress) {
  let xhr;
  const promise = new Promise((resolve, reject) => {
    const send = () => {
      const form = new FormData();
      for (const f of fileList) form.append("files", f, f.name);
      xhr = new XMLHttpRequest();
      xhr.open("POST", "api/sources");
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
      xhr.upload.onload = () => onProgress(1, 1, true);
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
        if (xhr.status === 401 && data.login) { needLogin().then(send, reject); return; }
        if (xhr.status < 200 || xhr.status >= 300) { reject(new Error(data.detail || "Upload failed. Please try again.")); return; }
        resolve(data.sources || []);
      };
      xhr.onerror = () => reject(new Error(NET_ERROR));
      xhr.onabort = () => reject(Object.assign(new Error("Upload stopped."), { aborted: true }));
      xhr.send(form);
    };
    send();
  });
  return { promise, abort: () => xhr && xhr.abort() };
}

/* ------------------------------------------------------------------ */
/* Login (only when the editor is protected by an access code)         */
/* ------------------------------------------------------------------ */

let loginWaiters = [];
function needLogin() {
  $("#login").hidden = false;
  setTimeout(() => $("#code-input").focus(), 50);
  return new Promise((resolve) => loginWaiters.push(resolve));
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = $("#code-input").value;
  if (!code) return;
  const btn = $("#code-btn");
  btn.disabled = true;
  try {
    const form = new URLSearchParams();
    form.append("code", code);
    const res = await fetch("api/login", { method: "POST", body: form, credentials: "same-origin" });
    let data = {};
    try { data = await res.json(); } catch (_) { /* ignore */ }
    if (!res.ok) throw new Error(data.detail || "That code did not work. Please try again.");
    $("#login-error").textContent = "";
    $("#code-input").value = "";
    $("#login").hidden = true;
    const w = loginWaiters; loginWaiters = [];
    w.forEach((r) => r());
  } catch (err) {
    $("#login-error").textContent = err.message === "Failed to fetch" ? NET_ERROR : err.message;
  } finally { btn.disabled = false; }
});

/* ------------------------------------------------------------------ */
/* Toast                                                               */
/* ------------------------------------------------------------------ */

let toastTimer = null;
function toast(text, { bad = false, action, onAction, ms = 3200 } = {}) {
  const t = $("#toast");
  t.replaceChildren(h("span", { text }));
  if (action) t.append(h("button", { text: action, onclick: () => { hideToast(); onAction(); } }));
  t.classList.toggle("bad", bad);
  t.hidden = false;
  requestAnimationFrame(() => t.classList.add("show"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, action ? ms + 2500 : ms);
}
function hideToast() {
  const t = $("#toast");
  t.classList.remove("show");
  setTimeout(() => { if (!t.classList.contains("show")) t.hidden = true; }, 220);
}
function updateToastSpot() {
  let b = 16;
  if (!$("#nav").classList.contains("away")) b = 84;
  if (R.screen === "project") b = (parseInt(getComputedStyle(document.documentElement).getPropertyValue("--chatbar-h")) || 120) + 10;
  if (R.screen === "new") b = 140;
  document.documentElement.style.setProperty("--toast-bottom", b + "px");
}

/* ------------------------------------------------------------------ */
/* Bottom sheet (Android back button closes it)                        */
/* ------------------------------------------------------------------ */

let sheet = null;
function openSheet(content, { onClose } = {}) {
  if (sheet) closeSheetNow();
  const body = $("#sheet-body");
  body.replaceChildren(content);
  body.scrollTop = 0;
  const root = $("#sheet");
  root.hidden = false;
  void root.offsetHeight;
  root.classList.add("open");
  sheet = { onClose, after: [] };
  history.pushState(Object.assign({}, history.state, { sheet: true }), "", location.hash);
}
function closeSheet() {
  return new Promise((resolve) => {
    if (!sheet) return resolve();
    sheet.after.push(resolve);
    if (history.state && history.state.sheet) history.back();
    else closeSheetNow();
  });
}
function closeSheetNow() {
  if (!sheet) return;
  const s = sheet;
  sheet = null;
  const root = $("#sheet");
  root.classList.remove("open");
  setTimeout(() => {
    if (sheet) return;
    root.hidden = true;
    const body = $("#sheet-body");
    if (body.firstChild && body.firstChild.id === "opts-panel") $("#holder").append(body.firstChild);
    else body.replaceChildren();
  }, 260);
  if (s.onClose) s.onClose();
  s.after.forEach((r) => r());
}
$("#sheet").addEventListener("click", (e) => { if (e.target.closest("[data-sheet-close]")) closeSheet(); });

function confirmBox({ title, text, ok = "OK", danger = false }) {
  return new Promise((resolve) => {
    let answered = false;
    const yes = h("button", { class: `btn btn-block ${danger ? "btn-danger" : "btn-primary"}`, text: ok });
    const no = h("button", { class: "btn btn-block", text: "Cancel" });
    yes.onclick = () => { answered = true; closeSheet().then(() => resolve(true)); };
    no.onclick = () => closeSheet();
    openSheet(h("div", { class: "confirm" }, h("h2", { text: title }), text ? h("p", { text }) : null,
      h("div", { class: "btn-row" }, no, yes)), { onClose: () => { if (!answered) resolve(false); } });
  });
}

function menuSheet(head, items) {
  const list = h("ul", { class: "menu" });
  for (const it of items) {
    if (!it) continue;
    const b = h("button", { class: it.danger ? "danger" : "", disabled: it.disabled || false },
      icon(it.icon), h("span", { class: "grow" }, it.label, it.sub ? h("small", { text: it.sub }) : null), it.right || null);
    b.onclick = () => closeSheet().then(it.run);
    list.append(h("li", null, b));
  }
  openSheet(h("div", null, head, list));
}

/* ------------------------------------------------------------------ */
/* Screens and the Android back button (history)                       */
/* ------------------------------------------------------------------ */

const R = { screen: null, params: {}, tok: 0, scroll: {} };
try { history.scrollRestoration = "manual"; } catch (_) { /* older WebView */ }
const NAV_SCREENS = ["home", "create", "settings"];

function hashFor(screen, params) {
  return "#/" + screen + (params && params.id ? "/" + encodeURIComponent(params.id) : "");
}
function parseHash() {
  const m = location.hash.match(/^#\/([a-z]+)(?:\/([^/]+))?/);
  const screen = m && SCREENS[m[1]] ? m[1] : "home";
  return { screen, params: m && m[2] ? { id: decodeURIComponent(m[2]) } : {} };
}

function go(screen, params = {}, mode = "push") {
  const cur = history.state || {};
  const depth = screen === "home" ? 0 : mode === "push" ? (cur.depth || 0) + 1 : cur.depth || 0;
  const st = { screen, params, depth };
  if (mode === "push") history.pushState(st, "", hashFor(screen, params));
  else history.replaceState(st, "", hashFor(screen, params));
  show(st, false);
}

function goTab(tab) {
  if (tab === R.screen) { window.scrollTo({ top: 0, behavior: "smooth" }); return; }
  const depth = (history.state && history.state.depth) || 0;
  if (tab === "home") { if (depth > 0) history.back(); else go("home", {}, "replace"); return; }
  go(tab, {}, R.screen === "home" ? "push" : "replace");
}

function goBack() {
  const depth = (history.state && history.state.depth) || 0;
  if (depth > 0) history.back();
  else go("home", {}, "replace");
}

window.addEventListener("popstate", (e) => {
  const st = e.state || parseHash();
  if (sheet) {
    closeSheetNow();
    if (st.screen === R.screen && ((st.params || {}).id || null) === (R.params.id || null)) return;
  }
  show(st, true);
});

function show(st, fromBack) {
  if (R.screen) R.scroll[R.screen] = window.scrollY;
  const leaving = R.screen && SCREENS[R.screen].leave;
  if (leaving) leaving();
  R.tok++;
  R.screen = st.screen;
  R.params = st.params || {};
  for (const s of Object.keys(SCREENS)) {
    const el = $("#s-" + s);
    const on = s === st.screen;
    el.hidden = !on;
    if (on) { el.classList.remove("enter"); void el.offsetWidth; el.classList.add("enter"); }
  }
  $("#make-bar").hidden = st.screen !== "new";
  const nav = $("#nav");
  nav.classList.toggle("away", !NAV_SCREENS.includes(st.screen));
  $$("#nav button").forEach((b) => {
    if (b.dataset.tab === st.screen) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  SCREENS[st.screen].enter(R.params, R.tok);
  window.scrollTo(0, fromBack ? R.scroll[st.screen] || 0 : 0);
  updateToastSpot();
  onScroll();
}

$("#nav").addEventListener("click", (e) => { const b = e.target.closest("button[data-tab]"); if (b) goTab(b.dataset.tab); });
document.addEventListener("click", (e) => { if (e.target.closest("[data-back]")) goBack(); });

function onScroll() {
  const y = window.scrollY > 4;
  $$(".appbar").forEach((a) => a.classList.toggle("scrolled", y));
}
window.addEventListener("scroll", onScroll, { passive: true });

/* Hide bars that would cover the keyboard (Android resizes the page). */
if (window.visualViewport) {
  let full = window.innerHeight;
  const check = () => {
    full = Math.max(full, window.visualViewport.height);
    const open = full - window.visualViewport.height > 150 && /^(INPUT|TEXTAREA)$/.test((document.activeElement || {}).tagName);
    document.body.classList.toggle("kb", open);
  };
  window.visualViewport.addEventListener("resize", check);
  window.addEventListener("focusout", () => setTimeout(check, 100));
}

/* ------------------------------------------------------------------ */
/* Shared state                                                        */
/* ------------------------------------------------------------------ */

const S = {
  settings: null,
  health: null,
  projects: null,
  styles: null,
  createResult: null,     // last finished AI creation {kind, files, format}
  createJob: store("createJob") || null,
};

const DEFAULT_OPTIONS = { format: "auto", length: "auto", style: "auto", look: "auto", transitions: "auto", slowmo: "auto",
  captions: "auto", caption_lang: "auto", caption_pos: "bottom", caption_style: "", title: "" };

const draft = {
  mode: "highlight",
  sources: [],      // Source objects from the API
  music: null,      // Source object
  request: "",
  options: Object.assign({}, DEFAULT_OPTIONS),
  busy: null,       // {kind: "visual"|"audio", text, loaded, total, abort}
  jobs: new Set(),  // jobs started from this draft (cleared when one finishes)
};

async function loadSettings() {
  try { S.settings = await api("api/settings"); } catch (_) { /* shown where needed */ }
  return S.settings;
}

/* ------------------------------------------------------------------ */
/* Home                                                                */
/* ------------------------------------------------------------------ */

function homeEnter(_p, tok) {
  if (!S.projects) renderProjectSkeleton();
  else renderProjects();
  loadProjects(tok);
}

function renderProjectSkeleton() {
  $("#proj-list").replaceChildren(...[0, 1, 2].map(() => h("li", { class: "skel skel-row" })));
}

const jobPeek = {}; // job id -> last known job (for progress on the home list)

async function loadProjects(tok) {
  try {
    const d = await api("api/projects");
    if (tok !== R.tok) return;
    S.projects = d.projects || [];
    renderProjects();
    const working = S.projects.filter((p) => p.state === "working" && p.job);
    await Promise.all(working.map(async (p) => {
      try { jobPeek[p.job] = await api(`api/jobs/${encodeURIComponent(p.job)}`); } catch (_) { /* ignore */ }
    }));
    if (tok !== R.tok) return;
    if (working.length) renderProjects();
    if (S.projects.some((p) => p.state === "working")) setTimeout(() => { if (tok === R.tok) loadProjects(tok); }, 2500);
  } catch (e) {
    if (tok !== R.tok) return;
    if (!S.projects) {
      $("#proj-list").replaceChildren(h("li", { class: "empty" },
        h("div", { class: "empty-ic" }, icon("alert")), h("b", { text: "Could not load your videos" }), h("p", { text: e.message }),
        h("button", { class: "btn", style: "margin-top:14px", onclick: () => { renderProjectSkeleton(); loadProjects(tok); } }, icon("retry"), "Try again")));
    }
  }
}

function renderProjects() {
  const list = $("#proj-list");
  const ps = S.projects || [];
  $("#proj-count").textContent = ps.length ? `${ps.length} video${ps.length > 1 ? "s" : ""}` : "";
  if (!ps.length) {
    list.replaceChildren(h("li", { class: "empty" },
      h("div", { class: "empty-ic" }, icon("film")),
      h("b", { text: "No videos yet" }),
      h("span", { text: "Pick one of the options above. Your edited videos will show up here." })));
    return;
  }
  list.replaceChildren(...ps.map(projectRow));
}

function projectRow(p) {
  const th = h("div", { class: "thumb" });
  if (p.thumb) th.append(img(p.thumb, ""));
  else th.append(h("div", { class: "thumb-ph" }, p.state === "working" ? h("div", { class: "spin" }) : icon("film")));
  if (p.duration && p.state !== "working") th.append(h("span", { class: "badge", text: fmtDur(p.duration) }));
  const job = p.job && jobPeek[p.job];
  if (p.state === "working" && job) th.append(h("div", { class: "tbar" }, h("i", { style: `width:${Math.round((job.progress || 0) * 100)}%` })));

  const meta = [MODE_NAMES[p.mode] || "Video", FORMAT_NAMES[p.format], fmtAgo(p.updated || p.created)].filter(Boolean).join(" · ");
  let state;
  if (p.state === "working") state = h("span", { class: "state working" }, h("span", { class: "dot" }),
    job && job.state === "working" ? `Working · ${Math.round((job.progress || 0) * 100)}%` : "Working…");
  else if (p.state === "failed") state = h("span", { class: "state failed" }, h("span", { class: "dot" }), "Didn't finish");
  else state = h("span", { class: "state ready" }, h("span", { class: "dot" }), "Ready");

  const main = h("button", { class: "proj-main", "aria-label": `Open ${p.title || "video"}` },
    th, h("div", { class: "proj-info" }, h("b", { text: p.title || "Untitled video" }), h("div", { class: "meta", text: meta }), state));
  const more = h("button", { class: "icon-btn", "aria-label": `More for ${p.title || "video"}` }, icon("more"));
  const li = h("li", { class: "proj" }, main, more);

  let pressTimer = null, longPressed = false;
  main.addEventListener("pointerdown", () => {
    longPressed = false;
    pressTimer = setTimeout(() => { longPressed = true; if (navigator.vibrate) navigator.vibrate(12); projectMenu(p, li); }, 520);
  });
  const cancelPress = () => clearTimeout(pressTimer);
  main.addEventListener("pointerup", cancelPress);
  main.addEventListener("pointerleave", cancelPress);
  main.addEventListener("pointercancel", cancelPress);
  main.addEventListener("contextmenu", (e) => e.preventDefault());
  main.addEventListener("click", (e) => { if (longPressed) { e.preventDefault(); return; } go("project", { id: p.id }); });
  more.onclick = () => projectMenu(p, li);
  return li;
}

function projectMenu(p, li) {
  const head = h("div", { class: "sheet-head" }, p.thumb ? img(p.thumb) : h("div", { class: "thumb" }, h("div", { class: "thumb-ph" }, icon("film"))),
    h("div", null, h("b", { text: p.title || "Untitled video" }), h("p", { text: [MODE_NAMES[p.mode], fmtDur(p.duration)].filter(Boolean).join(" · ") })));
  menuSheet(head, [
    { icon: "play", label: "Open", run: () => go("project", { id: p.id }) },
    { icon: "trash", label: "Delete", danger: true, run: () => deleteProject(p, li) },
  ]);
}

async function deleteProject(p, li) {
  const ok = await confirmBox({ title: "Delete this video?", text: `"${p.title || "Untitled video"}" and all its versions will be removed from the app. Videos you already saved to your phone stay there.`, ok: "Delete", danger: true });
  if (!ok) return false;
  try {
    await api(`api/projects/${encodeURIComponent(p.id)}`, { method: "DELETE" });
    if (li) { li.classList.add("leaving"); await sleep(200); }
    if (S.projects) S.projects = S.projects.filter((x) => x.id !== p.id);
    if (R.screen === "home") renderProjects();
    toast("Video deleted");
    return true;
  } catch (e) { toast(e.message, { bad: true }); return false; }
}

$$("[data-start]").forEach((b) => (b.onclick = () => { draft.mode = b.dataset.start; go("new"); }));

/* ------------------------------------------------------------------ */
/* New edit                                                            */
/* ------------------------------------------------------------------ */

const OPTION_GROUPS = [
  { name: "format", label: "Shape", cls: "two", items: [["auto", "Auto"], ["9:16", "Tall", "Reels, Shorts"], ["16:9", "Wide", "YouTube, TV"],
    ["1:1", "Square", "Feed posts"], ["original", "Original", "As filmed", "cleanup"]] },
  { name: "length", label: "Length", mode: "highlight", items: [["auto", "Auto"], ["15", "15 sec"], ["30", "30 sec"], ["60", "1 min"],
    ["90", "1½ min"], ["180", "3 min"]], cls: "three" },
  { name: "style", label: "Pace", items: [["auto", "Auto"], ["smooth", "Smooth", "Longer shots"], ["fast", "Fast", "Quick cuts"]], cls: "three" },
  { name: "look", label: "Colour look", swatch: true, cls: "three", items: [["auto", "Auto fix"], ["none", "Natural"], ["cinematic", "Cinematic"],
    ["vivid", "Vivid"], ["warm", "Warm"], ["cool", "Cool"], ["vintage", "Vintage"], ["bw", "Black & white"], ["dramatic", "Dramatic"]] },
  { name: "transitions", label: "Transitions", cls: "three", items: [["auto", "Auto"], ["soft", "Smooth"], ["dynamic", "Creative"],
    ["cinematic", "Cinematic"], ["cuts", "Straight cuts"]] },
  { name: "slowmo", label: "Slow motion", cls: "three", items: [["auto", "Auto"], ["on", "On"], ["off", "Off"]] },
  { name: "captions", label: "Captions", hint: "Words on screen from what people say", cls: "three", items: [["auto", "Auto"], ["on", "On"], ["off", "Off"]] },
  { name: "caption_lang", label: "Caption language", cap: true, cls: "three", items: [["auto", "As spoken"], ["en", "English"], ["hi", "हिंदी"],
    ["pa", "ਪੰਜਾਬੀ"], ["hinglish", "Hinglish"]] },
  { name: "caption_pos", label: "Caption position", cap: true, cls: "three", items: [["bottom", "Bottom"], ["middle", "Middle"], ["top", "Top"]] },
];
const SWATCH = {
  auto: "conic-gradient(#ff8a5b, #ffd35b, #5bffb0, #5bb8ff, #b15bff, #ff8a5b)", none: "linear-gradient(135deg,#9aa,#677)",
  cinematic: "linear-gradient(135deg,#1f6f78,#f39c5a)", vivid: "linear-gradient(135deg,#ff2d75,#ffd400)", warm: "linear-gradient(135deg,#ff9a3c,#ffd29a)",
  cool: "linear-gradient(135deg,#3c8dff,#9ae6ff)", vintage: "linear-gradient(135deg,#a07850,#e8d3a8)", bw: "linear-gradient(135deg,#111,#eee)",
  dramatic: "linear-gradient(135deg,#0b0b0b,#8a1c1c)",
};

function choiceGroup(items, value, onPick, { cls = "", swatch = false, label = "" } = {}) {
  const wrap = h("div", { class: "choices " + cls, role: "radiogroup", "aria-label": label });
  for (const [v, name, sub] of items) {
    const b = h("button", { class: "choice", role: "radio", "aria-checked": String(v === value), "data-value": v },
      swatch ? h("span", { class: "sw", style: `background:${SWATCH[v] || "#555"}` }) : null, name, sub ? h("small", { text: sub }) : null);
    b.onclick = () => {
      $$(".choice", wrap).forEach((x) => x.setAttribute("aria-checked", String(x === b)));
      onPick(v);
    };
    wrap.append(b);
  }
  return wrap;
}

function newEnter() {
  if (!S.settings) loadSettings().then(renderRequestHint);
  renderNew();
}

function renderNew() {
  $$("#mode-seg button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.mode === draft.mode)));
  $("#mode-tip").textContent = draft.mode === "cleanup"
    ? "Add your long video (5–30 minutes). The AI keeps everything in order and cuts out the boring bits."
    : "Add a few videos and photos. The AI picks the best moments.";
  $("#request").value = draft.request;
  $("#request").placeholder = draft.mode === "cleanup"
    ? "e.g. Remove the long pauses and the part where I drop the phone. Add captions in Hindi."
    : 'e.g. Make it fun and fast, 30 seconds, tall for Reels, title "Goa Trip"';
  if (draft.mode !== "cleanup" && draft.options.format === "original") draft.options.format = "auto";
  renderSources();
  renderMusic();
  renderRequestHint();
  renderOptsSummary();
}

function renderRequestHint() {
  const s = S.settings;
  $("#request-hint").textContent = s && s.can_text
    ? "Google AI reads your request, so write it any way you like."
    : 'Understands simple words like cinematic, tall, 30 seconds, slow motion, title "…". Add a Google AI key in Settings and it understands anything.';
}

$$("#mode-seg button").forEach((b) => (b.onclick = () => { draft.mode = b.dataset.mode; renderNew(); }));
$("#request").addEventListener("input", (e) => (draft.request = e.target.value));

const visualSources = () => draft.sources.filter((s) => !s.error);

function renderSources() {
  const grid = $("#src-grid");
  const kids = [];
  for (const s of draft.sources) kids.push(sourceTile(s));
  if (draft.busy && draft.busy.kind === "visual" && draft.busy.native) {
    for (let i = 0; i < Math.min(draft.busy.count || 1, 6); i++) kids.push(h("div", { class: "src loading skel" }, h("div", { class: "spin" })));
  }
  const busyVisual = draft.busy && draft.busy.kind === "visual";
  if (!draft.sources.length && !busyVisual) {
    kids.push(h("button", { class: "add-tile add-big", onclick: pickVisual }, icon("plus"),
      draft.mode === "cleanup" ? "Add your long video" : "Add videos & photos", h("small", { text: APP ? "From your phone's gallery" : "Choose files" })));
  } else if (!busyVisual) {
    kids.push(h("button", { class: "add-tile", onclick: pickVisual, "aria-label": "Add more videos and photos" }, icon("plus"), "Add"));
  }
  grid.replaceChildren(...kids);

  const ok = visualSources();
  const total = ok.reduce((t, s) => t + (s.kind === "video" ? s.duration || 0 : 0), 0);
  const nv = ok.filter((s) => s.kind === "video").length, np = ok.filter((s) => s.kind === "image").length;
  const parts = [];
  if (nv) parts.push(`${nv} video${nv > 1 ? "s" : ""}`);
  if (np) parts.push(`${np} photo${np > 1 ? "s" : ""}`);
  $("#src-count").textContent = parts.join(", ");
  $("#make-info").textContent = ok.length ? `${parts.join(" and ")}${total ? " · " + fmtDur(total) + " of video" : ""}` : "Add at least one video or photo to start";
  $("#make-btn").disabled = !ok.length || !!draft.busy;
  renderUploadCard();
}

function sourceTile(s) {
  const rm = h("button", { class: "x", "aria-label": `Remove ${s.name}` }, h("span", null, icon("x")));
  rm.onclick = () => { draft.sources = draft.sources.filter((x) => x !== s); renderSources(); };
  if (s.error) {
    const info = h("button", { class: "bad-msg", "aria-label": `${s.name} can't be used. Tap to see why.`, onclick: () => toast(s.error, { bad: true, ms: 5000 }) },
      icon("alert"), h("b", { text: "Can't open" }), h("span", { text: s.name }));
    return h("div", { class: "src bad", title: s.error }, info, rm);
  }
  const tile = h("div", { class: "src" });
  if (s.thumb) tile.append(img(s.thumb, s.name));
  else if (s._video) {
    const v = h("video", { src: s._video + "#t=0.5", preload: "metadata", muted: true, playsinline: true });
    v.muted = true;
    tile.append(v);
  } else tile.append(h("div", { class: "thumb-ph" }, icon(s.kind === "image" ? "image" : "video")));
  tile.append(h("span", { class: "badge" }, s.kind === "image" ? "Photo" : s.duration ? fmtDur(s.duration) : "Video"));
  tile.append(rm);
  return tile;
}

function renderUploadCard() {
  const card = $("#upload-card");
  const b = draft.busy;
  if (!b || b.native) { card.hidden = true; return; }
  card.hidden = false;
  const pct = b.total ? Math.min(100, Math.round((b.loaded / b.total) * 100)) : 0;
  const what = b.kind === "audio" ? "music" : `${b.count} file${b.count > 1 ? "s" : ""}`;
  const title = b.sent ? `Opening your ${b.kind === "audio" ? "music" : "files"}…` : `Uploading ${what} · ${pct}%`;
  const sub = b.sent ? "Checking length and picture size" : `${fmtSize(b.loaded)} of ${fmtSize(b.total)}`;
  const cancel = b.sent ? null : h("button", { class: "btn btn-sm btn-ghost", onclick: () => b.abort && b.abort() }, "Stop");
  card.replaceChildren(h("div", { class: "top" }, b.sent ? h("div", { class: "spin" }) : icon("upload"),
    h("div", null, h("b", { text: title }), h("span", { class: "muted", text: sub })), cancel),
    ...(b.sent ? [] : [h("div", { class: "bar" }, h("i", { style: `width:${pct}%` }))]));
}

function renderMusic() {
  const m = draft.music;
  const row = h("button", { class: "row-btn music-row" + (m ? " set" : ""), onclick: pickAudio },
    h("span", { class: "row-ic" }, icon("music")),
    h("span", { class: "row-txt" }, h("b", { text: m ? m.name : "Add music" }),
      h("small", { text: m ? (m.duration ? `${fmtDur(m.duration)} · tap to change` : "Tap to change") : "The AI picks the best part and fits it to your video" })),
    m ? h("span", { style: "width:40px" }) : icon("chev", "chev"));
  const wrap = h("div", { class: "music-wrap" }, row);
  if (m) {
    wrap.append(h("button", { class: "icon-btn", "aria-label": "Remove music", onclick: () => { draft.music = null; renderMusic(); } }, icon("x")));
  }
  if (draft.busy && draft.busy.kind === "audio" && draft.busy.native) {
    row.disabled = true;
    row.querySelector("small").textContent = "Opening your music…";
  }
  $("#music-row").replaceChildren(wrap);
}

function pickVisual() {
  if (draft.busy) { toast("Please wait until the files being added are ready"); return; }
  if (APP) {
    try { window.Android.pickMedia("visual"); } catch (e) { toast("Could not open the gallery.", { bad: true }); }
  } else $("#file-visual").click();
}
function pickAudio() {
  if (draft.busy) { toast("Please wait until the files being added are ready"); return; }
  if (APP) {
    try { window.Android.pickMedia("audio"); } catch (e) { toast("Could not open your music.", { bad: true }); }
  } else $("#file-audio").click();
}

function addSources(list, kind) {
  const bad = [];
  for (const s of list) {
    if (kind === "audio" || s.kind === "audio") {
      if (s.error) bad.push(s);
      else draft.music = s;
    } else draft.sources.push(s);
  }
  if (bad.length) toast(bad[0].error, { bad: true });
  const errs = list.filter((s) => s.error && s.kind !== "audio" && kind !== "audio").length;
  if (errs) toast(errs === 1 ? "1 file can't be opened (marked in red)" : `${errs} files can't be opened (marked in red)`, { bad: true });
}

// Browser mode: real upload with progress.
async function uploadPicked(files, kind) {
  if (!files.length) return;
  const total = files.reduce((t, f) => t + f.size, 0);
  const up = uploadFiles(files, (loaded, tot, sent) => {
    if (!draft.busy) return;
    if (sent) draft.busy.sent = true;
    else { draft.busy.loaded = loaded; draft.busy.total = tot; }
    if (R.screen === "new") renderUploadCard();
  });
  draft.busy = { kind, count: files.length, loaded: 0, total, abort: up.abort };
  refreshNewBusy();
  try {
    const list = await up.promise;
    draft.busy = null;
    addSources(list, kind);
  } catch (e) {
    draft.busy = null;
    if (!e.aborted) toast(e.message, { bad: true });
  }
  refreshNewBusy();
}
function refreshNewBusy() { if (R.screen === "new") { renderSources(); renderMusic(); } }

$("#file-visual").addEventListener("change", (e) => { const f = [...e.target.files]; e.target.value = ""; uploadPicked(f, "visual"); });
$("#file-audio").addEventListener("change", (e) => { const f = [...e.target.files]; e.target.value = ""; uploadPicked(f, "audio"); });

// App mode: the Android picker answers here.
window.onNativePicked = async function (res) {
  if (!res || res.cancelled) return;
  if (res.error) { toast(String(res.error), { bad: true }); return; }
  const items = (res.items || []).map((i) => ({ path: i.path, name: i.name }));
  if (!items.length) return;
  const kind = res.kind === "audio" ? "audio" : "visual";
  draft.busy = { kind, native: true, count: items.length };
  refreshNewBusy();
  try {
    const d = await api("api/sources/local", { method: "POST", json: { items: kind === "audio" ? items.slice(0, 1) : items } });
    draft.busy = null;
    addSources(d.sources || [], kind);
  } catch (e) {
    draft.busy = null;
    toast(e.message, { bad: true });
  }
  refreshNewBusy();
};

// App mode: videos or photos shared into the app from the gallery or another app.
window.onNativeShared = function (res) {
  if (!res || res.cancelled || res.error || !(res.items || []).length) return;
  if (R.screen !== "new") { draft.mode = draft.mode || "highlight"; go("new", {}, R.screen === "home" ? "push" : "replace"); }
  window.onNativePicked(Object.assign({}, res, { kind: "visual" }));
};

/* Options sheet */
function renderOptsSummary() {
  const o = draft.options;
  const changed = [];
  for (const g of OPTION_GROUPS) {
    if (g.mode && g.mode !== draft.mode) continue;
    if (o[g.name] !== DEFAULT_OPTIONS[g.name]) {
      const it = g.items.find((i) => i[0] === o[g.name]);
      if (it) changed.push(`${g.label}: ${it[1]}`);
    }
  }
  if (S.styles && o.caption_style && o.caption_style !== S.styles.default && o.captions !== "off") {
    const st = S.styles.styles.find((s) => s.id === o.caption_style);
    if (st) changed.push(`Caption style: ${st.name}`);
  }
  if (o.title.trim()) changed.push(`Title: "${o.title.trim()}"`);
  $("#opts-summary").textContent = changed.length ? changed.join(" · ") : "Everything on Auto";
}

function buildOptions() {
  const box = $("#opts-groups");
  box.replaceChildren();
  for (const g of OPTION_GROUPS) {
    if (g.mode && g.mode !== draft.mode) continue;
    const items = g.items.filter((i) => !i[3] || i[3] === draft.mode);
    const group = h("div", { class: "opt-group" + (g.cap ? " cap-dep" : ""), "data-name": g.name },
      h("h3", { class: "opt-label" }, g.label, g.hint ? h("span", { class: "opt-hint", text: g.hint }) : null),
      choiceGroup(items, draft.options[g.name], (v) => {
        draft.options[g.name] = v;
        if (g.name === "captions") updateCapDeps();
        if (g.name === "caption_lang") updateStylePreviews();
        renderOptsSummary();
      }, { cls: g.cls || "", swatch: g.swatch, label: g.label }));
    box.append(group);
  }
  $("#opt-title").value = draft.options.title;
  updateCapDeps();
  loadStyles();
}

function updateCapDeps() {
  const off = draft.options.captions === "off";
  $$(".cap-dep").forEach((el) => el.classList.toggle("dim", off));
  $("#cap-style-box").classList.toggle("dim", off);
}

$("#opt-title").addEventListener("input", (e) => { draft.options.title = e.target.value; renderOptsSummary(); });
$("#opts-reset").onclick = () => {
  const style = S.styles ? S.styles.default : "";
  draft.options = Object.assign({}, DEFAULT_OPTIONS, { caption_style: style });
  buildOptions();
  renderOptsSummary();
};
$("#more-opts").onclick = () => {
  buildOptions();
  openSheet($("#opts-panel"), { onClose: renderOptsSummary });
};

const styleLang = () => (draft.options.caption_lang === "auto" ? "en" : draft.options.caption_lang);

async function loadStyles() {
  const grid = $("#style-grid");
  if (!S.styles) {
    grid.replaceChildren(...Array.from({ length: 6 }, () => h("div", { class: "style skel", style: "aspect-ratio: 2.2" })));
    try { S.styles = await api("api/caption-styles"); } catch (e) {
      grid.replaceChildren(h("p", { class: "hint", text: e.message }));
      return;
    }
  }
  if (!draft.options.caption_style) draft.options.caption_style = S.styles.default;
  $("#style-count").textContent = `${S.styles.styles.length} styles`;
  const lang = styleLang();
  grid.replaceChildren(...S.styles.styles.map((st) => {
    const b = h("button", { class: "style", role: "radio", "aria-checked": String(st.id === draft.options.caption_style), "data-id": st.id },
      h("div", { class: "pv skel" }, img(`api/caption-preview/${encodeURIComponent(st.id)}?lang=${lang}`, "")),
      h("span", { text: st.name }), h("i", { class: "tick" }, icon("check")));
    b.onclick = () => {
      draft.options.caption_style = st.id;
      $$(".style", grid).forEach((x) => x.setAttribute("aria-checked", String(x === b)));
      renderOptsSummary();
    };
    return b;
  }));
  grid.setAttribute("role", "radiogroup");
  grid.setAttribute("aria-label", "Caption style");
}

function updateStylePreviews() {
  const lang = styleLang();
  $$("#style-grid .style").forEach((b) => {
    const i = b.querySelector("img");
    i.classList.remove("loaded");
    i.src = `api/caption-preview/${encodeURIComponent(b.dataset.id)}?lang=${lang}`;
  });
}

/* Make my video */
$("#make-btn").onclick = async () => {
  const ok = visualSources();
  if (!ok.length) return;
  const btn = $("#make-btn");
  btn.disabled = true;
  btn.replaceChildren(h("div", { class: "spin" }), h("span", { text: "Starting…" }));
  const o = draft.options;
  const options = {
    format: o.format, length: draft.mode === "highlight" ? o.length : "auto", style: o.style, look: o.look, transitions: o.transitions,
    slowmo: o.slowmo, title: o.title.trim(), captions: o.captions, caption_lang: o.caption_lang, caption_pos: o.caption_pos,
  };
  if (o.caption_style) options.caption_style = o.caption_style;
  try {
    const d = await api("api/projects", { method: "POST", json: {
      sources: ok.map((s) => s.id), music: draft.music ? draft.music.id : null, mode: draft.mode, request: draft.request.trim(), options } });
    const pid = d.project && typeof d.project === "object" ? d.project.id : d.project;
    draft.jobs.add(d.job);
    go("working", { id: d.job, project: pid, from: "new" }, "replace");
  } catch (e) {
    toast(e.message, { bad: true });
  } finally {
    btn.replaceChildren(icon("wand"), h("span", { text: "Make my video" }));
    btn.disabled = !visualSources().length;
  }
};

function resetDraft() {
  draft.sources = []; draft.music = null; draft.request = "";
  draft.options = Object.assign({}, DEFAULT_OPTIONS, { caption_style: S.styles ? S.styles.default : "" });
  draft.jobs.clear();
}

/* ------------------------------------------------------------------ */
/* Progress ring                                                       */
/* ------------------------------------------------------------------ */

function ring(small = false) {
  const r = 44, c = 2 * Math.PI * r;
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.innerHTML = `<circle class="track" cx="50" cy="50" r="${r}"/><circle class="val" cx="50" cy="50" r="${r}" stroke-dasharray="${c}" stroke-dashoffset="${c}"/>`;
  const pct = h("div", { class: "pct" });
  const el = h("div", { class: "ring" + (small ? " small" : ""), role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100" }, svg, pct);
  el.set = (frac, waiting) => {
    el.classList.toggle("wait", !!waiting);
    if (waiting) { pct.replaceChildren(); el.removeAttribute("aria-valuenow"); return; }
    const p = Math.max(0, Math.min(100, Math.round((frac || 0) * 100)));
    svg.querySelector(".val").setAttribute("stroke-dashoffset", String(c * (1 - p / 100)));
    pct.replaceChildren(String(p), h("small", { text: "%" }));
    el.setAttribute("aria-valuenow", String(p));
  };
  return el;
}

/* ------------------------------------------------------------------ */
/* Working (one job, full screen)                                      */
/* ------------------------------------------------------------------ */

const WORK_TITLES = { edit: "Editing your video", story: "Making your video", image: "Creating your pictures", video: "Creating your video clips", chat: "Changing your video" };

function workingEnter(p, tok) {
  const jobId = p.id;
  const box = $("#work-box");
  const rg = ring();
  rg.set(0, true);
  const title = h("h2", { class: "work-title", text: "Starting…" });
  const stage = h("p", { class: "work-stage", "aria-live": "polite" });
  const eta = h("p", { class: "work-eta" });
  const cancel = h("button", { class: "btn btn-ghost" }, icon("stop"), "Cancel");
  const leave = h("div", { class: "leave-card" }, icon("info"),
    h("div", null, h("b", { text: "You can leave the app" }),
      h("span", { text: "It keeps working. Your video will be waiting in My videos on the Home screen." })));
  box.replaceChildren(rg, title, stage, eta, leave, cancel);
  $("#s-working .appbar h1").textContent = "";
  let job = null, fails = 0, stopped = false;

  cancel.onclick = async () => {
    const ok = await confirmBox({ title: "Stop making this?", text: "The work done so far will be thrown away.", ok: "Stop", danger: true });
    if (!ok || tok !== R.tok) return;
    cancel.disabled = true;
    try { await api(`api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }); } catch (e) { toast(e.message, { bad: true }); cancel.disabled = false; }
  };

  const poll = async () => {
    if (tok !== R.tok || stopped) return;
    try {
      job = await api(`api/jobs/${encodeURIComponent(jobId)}`);
      fails = 0;
    } catch (e) {
      if (tok !== R.tok) return;
      if (e.status === 404) return showFail(e.message || "This job no longer exists.");
      fails++;
      if (fails > 3) stage.textContent = "Reconnecting…";
      setTimeout(poll, 1500);
      return;
    }
    if (tok !== R.tok) return;
    title.textContent = job.state === "waiting" ? "Waiting to start…" : WORK_TITLES[job.kind] || "Working…";
    if (job.kind === "image" || job.kind === "video") leave.querySelector("span").textContent = "It keeps working. Come back to the Create tab to see the result.";
    if (job.state === "done") return finished(job);
    if (job.state === "failed") return showFail(job.error || "Something went wrong.");
    if (job.state === "cancelled") return stoppedJob();
    rg.set(job.progress, job.state === "waiting");
    stage.textContent = job.stage ? job.stage + "…" : "";
    eta.textContent = job.state === "working" ? fmtEta(job.eta) : "";
    setTimeout(poll, 700);
  };

  const finished = (job) => {
    rg.set(1);
    stopped = true;
    const r = job.result || {};
    draft.jobs.has(jobId) && resetDraft();
    if (job.kind === "image" || job.kind === "video") {
      S.createResult = { kind: job.kind, files: r.files || [], format: S.lastCreateFormat || "9:16" };
      setCreateJob(null);
      go("create", {}, "replace");
      return;
    }
    if (S.createJob === jobId) setCreateJob(null);
    const pid = job.project || r.project || p.project;
    if (pid) { S.projects = null; go("project", { id: pid }, "replace"); }
    else go("home", {}, "replace");
  };

  const stoppedJob = () => {
    stopped = true;
    if (S.createJob === jobId) setCreateJob(null);
    toast("Stopped");
    const back = job && (job.kind === "image" || job.kind === "video" || job.kind === "story") ? "create" : draft.sources.length ? "new" : "home";
    go(back, {}, "replace");
  };

  const showFail = (msg) => {
    stopped = true;
    if (S.createJob === jobId) setCreateJob(null);
    const fromCreate = job && (job.kind === "image" || job.kind === "video" || job.kind === "story");
    const retry = h("button", { class: "btn btn-primary btn-lg" }, icon("retry"), "Try again");
    retry.onclick = () => go(fromCreate ? "create" : visualSources().length ? "new" : "home", {}, "replace");
    const home = h("button", { class: "btn" }, "Back to home");
    home.onclick = () => goBack();
    box.replaceChildren(h("div", { class: "fail-ic" }, icon("alert")), h("h2", { class: "work-title", text: "That didn't work" }),
      h("p", { class: "fail-msg", text: msg }), retry, home);
  };

  poll();
}

/* ------------------------------------------------------------------ */
/* Project / editor                                                    */
/* ------------------------------------------------------------------ */

const P = {
  data: null,       // Project from the API
  job: null,        // {id, kind, local:"chat"|"other", last: job}
  pending: null,    // user's message while a chat job runs (shown at once)
  localErr: null,   // error text shown as an assistant bubble
  selClip: null,
  els: {},
};

function projectEnter(p, tok) {
  const id = p.id;
  if (!P.data || P.data.id !== id) {
    P.data = null; P.job = null; P.pending = null; P.localErr = null; P.selClip = null;
    $("#p-bar-title").textContent = "";
    $("#p-body").replaceChildren(h("div", { class: "player skel", style: "aspect-ratio:9/16; width:min(100%, calc(55vh * 0.5625))" }),
      h("div", { class: "skel skel-line", style: "width:60%;height:22px;margin-top:18px" }), h("div", { class: "skel skel-line", style: "width:40%" }));
    removeChatbar();
  } else renderProject();
  loadProject(id, tok);
}

function projectLeave() {
  const v = $("#pv");
  if (v) v.pause();
  removeChatbar();
}

async function loadProject(id, tok) {
  try {
    const d = await api(`api/projects/${encodeURIComponent(id)}`);
    if (tok !== R.tok) return;
    const prevVersion = P.data && P.data.id === id ? P.data.version : null;
    P.data = d;
    renderProject();
    if (d.job && (!P.job || P.job.id !== d.job)) trackJob(d.job, null, tok);
    return prevVersion;
  } catch (e) {
    if (tok !== R.tok) return;
    if (!P.data) {
      $("#p-body").replaceChildren(h("div", { class: "empty", style: "margin-top:20px" }, h("div", { class: "empty-ic" }, icon("alert")),
        h("b", { text: "Could not open this video" }), h("p", { text: e.message }),
        h("button", { class: "btn", style: "margin-top:14px", onclick: () => loadProject(id, tok) }, icon("retry"), "Try again")));
    } else toast(e.message, { bad: true });
  }
}

function renderProject() {
  const d = P.data;
  const plan = d.plan || {};
  $("#p-bar-title").textContent = d.title || "Your video";
  const body = $("#p-body");
  const keepVideo = $("#pv");

  // Player
  const w = plan.width || 9, hgt = plan.height || 16;
  const ratio = w / hgt;
  const player = h("div", { class: "player", id: "p-player", style: `aspect-ratio:${w}/${hgt}; width:min(100%, calc(55vh * ${ratio.toFixed(4)}))` });
  if (d.video) {
    let v = keepVideo;
    if (!v) {
      v = h("video", { id: "pv", controls: true, playsinline: true, preload: "metadata" });
      v.addEventListener("timeupdate", markPlayingClip);
    }
    if (v.getAttribute("data-src") !== d.video) {
      v.setAttribute("data-src", d.video);
      v.src = d.video;
      if (d.thumb) v.poster = d.thumb;
    }
    player.append(v);
  } else {
    player.classList.add("empty");
    if (d.state === "failed") player.append(h("div", { class: "overlay", style: "background:transparent" }, h("div", { class: "fail-ic" }, icon("alert")),
      h("b", { text: "This video didn't finish" }), h("p", { text: d.error || "Something went wrong." }),
      h("button", { class: "btn btn-primary", onclick: () => go("new", {}, "replace") }, icon("plus"), "Start a new edit")));
  }
  P.els.player = player;

  // Title + version
  const total = (d.versions || []).find((v) => v.n === d.version);
  const dur = total ? total.duration : plan.total;
  const meta = [d.video ? fmtDur(dur) : "", FORMAT_NAMES[plan.format], MODE_NAMES[d.mode]].filter(Boolean).join(" · ");
  const vchip = d.version ? h("button", { class: "vchip", "aria-label": `Version ${d.version}, show all versions`, onclick: versionSheet },
    icon("layers"), `Version ${d.version}`, icon("down")) : null;
  const head = h("div", { class: "p-head" }, h("div", null, h("h2", { class: "p-title", text: d.title || "Your video" }), h("p", { class: "p-meta", text: meta })), vchip);

  // Actions
  const busy = !!P.job;
  let actions = null;
  if (d.video) {
    const save = h("button", { class: "btn btn-primary" }, icon("download"), "Save to phone");
    save.onclick = saveVideo;
    const share = APP ? h("button", { class: "btn sq", "aria-label": "Share" }, icon("share")) : null;
    if (share) share.onclick = () => { try { window.Android.share(d.download, safeName(d.title) + ".mp4", "video/mp4"); } catch (e) { toast("Could not open sharing.", { bad: true }); } };
    const undo = h("button", { class: "btn" + (APP ? " sq" : ""), "aria-label": "Undo last change", disabled: busy || !(d.version > 1) }, icon("undo"), APP ? null : "Undo");
    undo.onclick = doUndo;
    actions = h("div", { class: "p-actions", style: APP ? "" : "grid-template-columns:1fr auto" }, save, share, undo);
  }

  // Timeline
  const clips = plan.clips || [];
  let timeline = null;
  if (clips.length && d.video) {
    const strip = h("div", { class: "timeline", role: "list" });
    for (const c of clips) {
      const outDur = c.duration / (c.speed || 1);
      const width = Math.round(Math.max(64, Math.min(150, outDur * 22)));
      const b = h("button", { class: "clip" + (P.selClip === c.index ? " on" : ""), style: `width:${width}px`, role: "listitem",
        "aria-label": `Clip ${c.index + 1}, ${fmtDur(outDur)}`, "data-i": c.index },
        img(c.thumb || `api/projects/${encodeURIComponent(d.id)}/clips/${c.index}/thumb`, ""),
        h("span", { class: "badge num", text: String(c.index + 1) }),
        c.speed && c.speed !== 1 ? h("span", { class: "badge spd", text: c.speed < 1 ? "Slow" : "Fast" }) : null,
        h("span", { class: "badge", text: fmtDur(outDur) }));
      b.onclick = () => clipTap(c);
      strip.append(b);
    }
    timeline = h("section", { class: "sec" }, h("div", { class: "sec-title" }, h("h2", { text: "Clips" }), h("span", { class: "muted", text: `${clips.length} · tap one to change it` })), strip);
  }

  // Texts
  let texts = null;
  if ((plan.texts || []).length && d.video) {
    texts = h("section", { class: "sec" }, h("div", { class: "sec-title" }, h("h2", { text: "Text on screen" })),
      h("ul", { class: "texts" }, plan.texts.map((t) => h("li", null, icon("type"), h("span", { text: t.text }), h("small", { text: `${fmtDur(t.start)}–${fmtDur(t.end)}` })))));
  }

  // What the AI did
  let did = null;
  const facts = [];
  if (plan.look && plan.look !== "none") facts.push(`Look: ${(OPTION_GROUPS[3].items.find((i) => i[0] === plan.look) || [0, plan.look])[1]}`);
  if (plan.transition_kind) facts.push(`Transitions: ${(OPTION_GROUPS[4].items.find((i) => i[0] === plan.transition_kind) || [0, plan.transition_kind])[1]}`);
  if (plan.music && plan.music.name) facts.push(`Music: ${plan.music.name}`);
  if (clips.length) facts.push(`${clips.length} clips`);
  if (d.video && ((plan.notes || []).length || facts.length)) {
    did = h("details", { class: "did sec" }, h("summary", null, icon("sparkles"), h("span", { text: "What the AI did" }), icon("down", "tog")),
      facts.length ? h("div", { class: "facts" }, facts.map((f) => h("span", { text: f }))) : null,
      (plan.notes || []).length ? h("ul", null, plan.notes.map((n) => h("li", { text: n }))) : null);
    if (P.didOpen) did.open = true;
    did.addEventListener("toggle", () => (P.didOpen = did.open));
  }

  // Chat
  const chat = h("section", { class: "sec", id: "p-chat-sec" }, h("div", { class: "sec-title" }, h("h2", { text: "Chat with the AI" })));
  if (d.sources_available === false) {
    chat.append(h("div", { class: "note" }, icon("info"), h("span", null,
      "The original files are no longer loaded (the app was restarted). To change this video again, add the original files in a new edit. You can still ask questions here.")));
  }
  const list = h("div", { class: "chat", id: "p-chat", "aria-live": "polite" });
  chat.append(list);
  P.els.chat = list;

  body.replaceChildren(...[player, head, actions, timeline, texts, did, chat].filter(Boolean));
  renderChat();
  renderJobUI();
  ensureChatbar();
  markPlayingClip();
}

function renderChat() {
  const d = P.data, list = P.els.chat;
  if (!list) return;
  const kids = [];
  const msgs = d.chat || [];
  if (!msgs.length && !P.pending) {
    kids.push(d.sources_available === false
      ? h("div", { class: "chat-intro" }, h("b", { text: "Ask the AI about this video" }), 'For example: "how long is it?" or "which parts did you keep?"')
      : h("div", { class: "chat-intro" }, h("b", { text: "Tell the AI what to change" }),
        'Like you would tell a friend: "make it shorter", "remove the part where I cough", "add the title Goa Trip at the start", "use warmer colours".'));
  }
  const last = msgs[msgs.length - 1];
  const errInChat = !!(P.localErr && last && last.role === "assistant" && last.text === P.localErr);
  for (const m of msgs) {
    const user = m.role === "user";
    const bub = h("div", { class: "msg " + (user ? "user" : "ai") + (errInChat && m === last ? " err" : "") }, m.text);
    if (!user && m.version) bub.append(h("div", null, h("span", { class: "vtag" }, icon("layers"), `Version ${m.version}`)));
    kids.push(bub);
  }
  if (P.pending) kids.push(h("div", { class: "msg user" }, P.pending));
  if (P.job && P.job.local === "chat") kids.push(busyBubble());
  if (P.localErr && !errInChat) kids.push(h("div", { class: "msg ai err" }, P.localErr));
  list.replaceChildren(...kids);
}

function busyBubble() {
  const j = P.job.last;
  const pct = j && j.state === "working" ? Math.round((j.progress || 0) * 100) : null;
  const top = h("div", { class: "busy-top" }, h("span", null, j && j.state === "working" ? "Working on it" : "Reading your message"),
    pct != null ? h("span", { text: pct + "%" }) : h("span", { class: "typing" }, h("i"), h("i"), h("i")));
  const b = h("div", { class: "msg ai busy", id: "busy-bubble" }, top);
  if (j && j.stage) b.append(h("div", { class: "muted", text: j.stage + (j.eta != null && j.state === "working" ? " · " + fmtEta(j.eta).toLowerCase() : "") }));
  if (pct != null) b.append(h("div", { class: "bar" }, h("i", { style: `width:${pct}%` })));
  return b;
}

function renderJobUI() {
  const player = P.els.player;
  if (!player) return;
  const old = player.querySelector(".overlay.job");
  if (old) old.remove();
  if (P.job && P.job.local !== "chat") {
    const j = P.job.last;
    const rg = ring(true);
    rg.set(j ? j.progress : 0, !j || j.state === "waiting");
    const ov = h("div", { class: "overlay job" }, rg,
      h("b", { text: !P.data.video ? WORK_TITLES[(j && j.kind) || "edit"] || "Working…" : "Making the change…" }),
      h("p", { text: [(j && j.stage) || "", j && j.state === "working" ? fmtEta(j.eta) : ""].filter(Boolean).join(" · ") }));
    if (!P.data.video) ov.style.background = "transparent";
    player.append(ov);
  }
  const bar = $("#chatbar");
  if (bar) {
    $$("button, textarea", bar).forEach((el) => (el.disabled = !!P.job));
    updateSendBtn();
    $("#chat-text").placeholder = P.job ? "Please wait, the AI is working…" : P.data.sources_available === false ? "Ask the AI a question…" : "Tell the AI what to change…";
  }
  $$(".p-actions .btn").forEach((b) => { if (b.getAttribute("aria-label") === "Undo last change") b.disabled = !!P.job || !(P.data.version > 1); });
}

/* Polls a project's job; chat jobs show in a bubble, others over the player. */
async function trackJob(jobId, local, tok) {
  P.job = { id: jobId, local, last: null };
  renderChat(); renderJobUI();
  let fails = 0;
  while (tok === R.tok && P.job && P.job.id === jobId) {
    let j;
    try { j = await api(`api/jobs/${encodeURIComponent(jobId)}`); fails = 0; } catch (e) {
      if (e.status === 404) { j = { state: "failed", error: e.message }; } else { fails++; await sleep(Math.min(4000, 700 * fails)); continue; }
    }
    if (tok !== R.tok || !P.job || P.job.id !== jobId) return;
    if (!P.job.local) P.job.local = j.kind === "chat" && P.data && P.data.video ? "chat" : "other";
    P.job.last = j;
    if (["done", "failed", "cancelled"].includes(j.state)) {
      await jobFinished(j, tok);
      return;
    }
    if (P.job.local === "chat") {
      const bb = $("#busy-bubble");
      if (bb) bb.replaceWith(busyBubble()); else renderChat();
    } else renderJobUI();
    await sleep(700);
  }
}

async function jobFinished(j, tok) {
  const before = P.data ? P.data.version : null;
  const wasChat = !!(P.job && P.job.local === "chat");
  P.job = null;
  P.pending = null;
  await loadProject(P.data.id, tok);
  if (tok !== R.tok) return;
  if (j.state === "failed") {
    P.localErr = j.error || "Sorry, that did not work.";
    renderChat();
    if (!wasChat) toast(j.error || "That did not work.", { bad: true });
  } else if (j.state === "cancelled") {
    toast("Stopped");
  } else if (P.data.version && P.data.version !== before) {
    toast(`Version ${P.data.version} is ready`, { action: "Watch", onAction: () => {
      window.scrollTo({ top: 0, behavior: "smooth" });
      const v = $("#pv"); if (v) { v.currentTime = 0; v.play().catch(() => {}); }
    } });
  }
  renderJobUI();
  scrollChatEnd();
}

function scrollChatEnd() {
  requestAnimationFrame(() => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" }));
}

/* Chat bar (fixed at the bottom of the editor) */
function ensureChatbar() {
  let bar = $("#chatbar");
  if (!bar) {
    const ta = h("textarea", { id: "chat-text", rows: "1", maxlength: "1000", "aria-label": "Message to the AI", placeholder: "Tell the AI what to change…", enterkeyhint: "send" });
    const send = h("button", { class: "send", id: "chat-send", "aria-label": "Send", disabled: true }, icon("send"));
    bar = h("div", { class: "chatbar", id: "chatbar" }, h("div", { class: "chips-row", id: "chat-chips" }), h("div", { class: "chat-input" }, ta, send));
    document.body.append(bar);
    ta.addEventListener("input", () => { autosize(ta); updateSendBtn(); });
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !("ontouchstart" in window)) { e.preventDefault(); sendChat(ta.value); } });
    send.onclick = () => sendChat(ta.value);
    new ResizeObserver(() => {
      document.documentElement.style.setProperty("--chatbar-h", bar.offsetHeight + "px");
      updateToastSpot();
    }).observe(bar);
  }
  const chips = $("#chat-chips");
  const sug = P.data && P.data.sources_available !== false ? P.data.suggestions || [] : [];
  chips.replaceChildren(...sug.map((s) => h("button", { text: s, disabled: !!P.job, onclick: () => sendChat(s) })));
  chips.hidden = !sug.length;
}
function removeChatbar() { const b = $("#chatbar"); if (b) b.remove(); }
function autosize(ta) { ta.style.height = "auto"; ta.style.height = Math.min(140, ta.scrollHeight + 2) + "px"; }
function updateSendBtn() { const ta = $("#chat-text"), s = $("#chat-send"); if (ta && s) s.disabled = !!P.job || !ta.value.trim(); }

async function sendChat(text) {
  text = (text || "").trim();
  if (!text || P.job || !P.data) return;
  const ta = $("#chat-text");
  const tok = R.tok;
  P.pending = text;
  P.localErr = null;
  P.job = { id: null, local: "chat", last: null };
  if (ta) { ta.value = ""; autosize(ta); ta.blur(); }
  renderChat(); renderJobUI(); ensureChatbar();
  scrollChatEnd();
  try {
    const d = await api(`api/projects/${encodeURIComponent(P.data.id)}/chat`, { method: "POST", json: { message: text } });
    if (tok !== R.tok) return;
    trackJob(d.job, "chat", tok);
  } catch (e) {
    if (tok !== R.tok) return;
    P.job = null;
    P.localErr = e.message;
    if (ta && !ta.value) ta.value = text;
    P.pending = null;
    renderChat(); renderJobUI(); ensureChatbar();
    scrollChatEnd();
  }
}

async function startProjectJob(path, json, msg) {
  if (P.job) return;
  const tok = R.tok;
  P.localErr = null;
  P.job = { id: null, local: "other", last: null };
  renderJobUI(); ensureChatbar();
  window.scrollTo({ top: 0, behavior: "smooth" });
  try {
    const d = await api(path, { method: "POST", json });
    if (tok !== R.tok) return;
    trackJob(d.job, "other", tok);
  } catch (e) {
    P.job = null;
    renderJobUI(); ensureChatbar();
    toast(e.message || msg, { bad: true });
  }
}

function doUndo() {
  startProjectJob(`api/projects/${encodeURIComponent(P.data.id)}/undo`, {}, "Could not undo.");
}

function versionSheet() {
  const d = P.data;
  const vs = [...(d.versions || [])].sort((a, b) => b.n - a.n);
  menuSheet(h("div", { class: "sheet-title" }, h("h2", { text: "Versions" })), vs.map((v) => ({
    icon: v.n === d.version ? "check" : "layers",
    label: `Version ${v.n}${v.label ? " · " + v.label : ""}`,
    sub: [fmtDur(v.duration), fmtAgo(v.created)].filter(Boolean).join(" · "),
    right: v.n === d.version ? h("span", { class: "now", text: "Showing" }) : null,
    disabled: !!P.job,
    run: () => { if (v.n !== d.version) startProjectJob(`api/projects/${encodeURIComponent(d.id)}/restore`, { version: v.n }, "Could not go back to that version."); },
  })));
}

function saveVideo() {
  const d = P.data;
  if (!d.download) return;
  const name = safeName(d.title) + ".mp4";
  if (APP) {
    try { window.Android.saveToGallery(d.download, name, "video/mp4"); toast("Saving to your phone…"); }
    catch (e) { toast("Could not save the video.", { bad: true }); }
  } else {
    const a = h("a", { href: d.download, download: name });
    document.body.append(a); a.click(); a.remove();
    toast("Download started");
  }
}
window.onNativeSaved = function (res) {
  res = res || {};
  toast(res.message || (res.ok ? "Saved to your phone" : "Could not save."), { bad: !res.ok });
};

function markPlayingClip() {
  const v = $("#pv");
  if (!v || !P.data || !P.data.plan) return;
  const t = v.currentTime;
  let cur = null;
  for (const c of P.data.plan.clips || []) if (t + 0.05 >= c.out_start) cur = c.index;
  $$(".clip").forEach((b) => b.classList.toggle("playing", Number(b.dataset.i) === cur && !v.paused));
}

function clipTap(c) {
  const d = P.data;
  P.selClip = c.index;
  $$(".clip").forEach((b) => b.classList.toggle("on", Number(b.dataset.i) === c.index));
  const v = $("#pv");
  if (v) {
    const seek = () => { try { v.currentTime = (c.out_start || 0) + 0.05; } catch (_) { /* ignore */ } };
    if (v.readyState >= 1) seek(); else v.addEventListener("loadedmetadata", seek, { once: true });
  }
  const clips = d.plan.clips;
  const pos = clips.findIndex((x) => x.index === c.index);
  const outDur = c.duration / (c.speed || 1);
  const locked = d.sources_available === false;
  const slow = c.speed && c.speed < 1;
  const head = h("div", null, h("div", { class: "sheet-head" }, img(c.thumb || `api/projects/${encodeURIComponent(d.id)}/clips/${c.index}/thumb`),
    h("div", null, h("b", { text: `Clip ${pos + 1} of ${clips.length}` }), h("p", { text: `${fmtDur(outDur)} long · starts at ${fmtDur(c.out_start)}${slow ? " · slow motion" : ""}` }))),
    locked ? h("div", { class: "note" }, icon("info"), h("span", { text: "The original files are no longer loaded, so clips can't be changed. Start a new edit with the same files to change them." })) : null);
  const act = (action, extra = {}) => () => startProjectJob(`api/projects/${encodeURIComponent(d.id)}/clips`, Object.assign({ action, clip: c.index }, extra), "Could not change the clip.");
  menuSheet(head, [
    { icon: "trash", label: "Remove this clip", danger: true, disabled: locked || clips.length < 2, run: act("remove") },
    { icon: "left", label: "Move left", sub: "Play it earlier", disabled: locked || pos === 0, run: act("move", { to: pos - 1 }) },
    { icon: "right", label: "Move right", sub: "Play it later", disabled: locked || pos === clips.length - 1, run: act("move", { to: pos + 1 }) },
    slow ? { icon: "gauge", label: "Normal speed", disabled: locked, run: act("normal_speed") }
      : { icon: "slow", label: "Slow motion", disabled: locked || c.kind === "image", run: act("slowmo") },
    { icon: "zoom", label: "Zoom in", sub: "Gently move closer", disabled: locked, run: act("zoom") },
  ]);
}

$("#p-menu").onclick = () => {
  const d = P.data;
  if (!d) return;
  menuSheet(h("div", { class: "sheet-title" }, h("h2", { text: d.title || "Your video" })), [
    d.download ? { icon: "download", label: "Save to phone", run: saveVideo } : null,
    { icon: "plus", label: "Start a new edit", run: () => { go("new", {}, "replace"); } },
    { icon: "trash", label: "Delete this video", danger: true, run: async () => {
      if (await deleteProject({ id: d.id, title: d.title })) { P.data = null; goBack(); }
    } },
  ]);
};

/* ------------------------------------------------------------------ */
/* Create with AI                                                      */
/* ------------------------------------------------------------------ */

const AI = { mode: "image", format: "9:16", source: "pictures", count: "1", prompt: "", request: "" };

function setCreateJob(id) { S.createJob = id; store("createJob", id); }

async function createEnter(_p, tok) {
  if (!S.settings) {
    $("#create-body").replaceChildren(h("div", { class: "skel", style: "height:96px;border-radius:18px" }), h("div", { class: "skel", style: "height:120px;border-radius:16px;margin-top:14px" }));
    await loadSettings();
    if (tok !== R.tok) return;
  } else loadSettings().then(() => { if (tok === R.tok) renderCreate(tok); });
  renderCreate(tok);
}

function renderCreate(tok) {
  const body = $("#create-body");
  const s = S.settings;
  if (!s) {
    body.replaceChildren(h("div", { class: "empty" }, h("div", { class: "empty-ic" }, icon("alert")), h("b", { text: "Could not load settings" }),
      h("button", { class: "btn", style: "margin-top:14px", onclick: () => createEnter({}, tok) }, icon("retry"), "Try again")));
    return;
  }
  if (!s.ai_ready) {
    body.replaceChildren(h("div", { class: "card ai-off" }, h("div", { class: "empty-ic" }, icon("key")), h("h2", { text: "Turn on AI creation" }),
      h("p", { text: "Making pictures and videos from a description uses Google's AI. It needs your own Google AI key, added once in Settings. It's free to get one." }),
      h("button", { class: "btn btn-primary btn-block btn-lg", onclick: () => go("settings", {}, "replace") }, icon("key"), "Open Settings")));
    return;
  }
  const kids = [];

  if (S.createJob) {
    const mj = h("button", { class: "mini-job", onclick: () => go("working", { id: S.createJob }, "replace") }, h("div", { class: "spin" }),
      h("span", null, h("b", { text: "Still creating…" }), h("small", { text: "Tap to see progress" })), icon("chev", "chev"));
    kids.push(mj);
    api(`api/jobs/${encodeURIComponent(S.createJob)}`).then((j) => {
      if (tok !== R.tok) return;
      if (j.state === "working" || j.state === "waiting") mj.querySelector("small").textContent = `${Math.round((j.progress || 0) * 100)}% · ${j.stage || "working"} · tap to see progress`;
      else if (j.state === "done") {
        setCreateJob(null);
        if (j.kind === "image" || j.kind === "video") { S.createResult = { kind: j.kind, files: (j.result || {}).files || [], format: AI.format }; renderCreate(tok); }
        else if (j.project || (j.result || {}).project) mj.replaceWith(h("button", { class: "mini-job", onclick: () => go("project", { id: j.project || j.result.project }) },
          icon("film"), h("span", null, h("b", { text: "Your AI video is ready" }), h("small", { text: "Tap to open it" })), icon("chev", "chev")));
      } else { setCreateJob(null); mj.remove(); }
    }).catch(() => {});
  }

  if (S.createResult && S.createResult.files.length) kids.push(resultsBlock());

  const kinds = [["image", "Picture", "image", "One image"], ["video", "Video clip", "video", "About 8 sec"], ["story", "Full video", "film", "From an idea"]];
  const kindRow = h("div", { class: "kind-cards", role: "radiogroup", "aria-label": "What to make" });
  for (const [v, name, ic, sub] of kinds) {
    const b = h("button", { class: "kind", role: "radio", "aria-checked": String(AI.mode === v) }, icon(ic), name, h("small", { text: sub }));
    b.onclick = () => { AI.mode = v; renderCreate(tok); };
    kindRow.append(b);
  }
  kids.push(h("label", { class: "form-label", text: "What do you want to make?" }), kindRow);

  const ta = h("textarea", { id: "prompt", rows: "4", maxlength: "2000",
    placeholder: AI.mode === "story" ? "e.g. A short film about a kite festival in Punjab at sunset, families on rooftops, colourful kites" :
      "e.g. A golden sunset over a quiet beach with palm trees, waves rolling in" });
  ta.value = AI.prompt;
  ta.addEventListener("input", () => (AI.prompt = ta.value));
  kids.push(h("label", { class: "form-label", for: "prompt", text: "Describe it" }), ta);

  const videoish = AI.mode === "video" || (AI.mode === "story" && AI.source === "clips");
  if (videoish && AI.format === "1:1") AI.format = "9:16";
  const maxCount = videoish ? 3 : 4;
  if (Number(AI.count) > maxCount) AI.count = String(maxCount);
  const shapes = [["9:16", "Tall", "9:16"], ["16:9", "Wide", "16:9"]];
  if (!videoish) shapes.push(["1:1", "Square", "1:1"]);
  kids.push(h("label", { class: "form-label", text: "Shape" }), choiceGroup(shapes, AI.format, (v) => (AI.format = v), { cls: "three", label: "Shape" }));

  if (AI.mode === "story") {
    kids.push(h("label", { class: "form-label", text: "Build the video from" }),
      choiceGroup([["pictures", "AI pictures", "Cheaper, motion added"], ["clips", "AI video clips", "Real motion, costs more"]], AI.source,
        (v) => { AI.source = v; renderCreate(tok); }, { cls: "two", label: "Build the video from" }));
    const rq = h("input", { type: "text", id: "story-request", maxlength: "300", placeholder: 'e.g. cinematic, title "Dreams"' });
    rq.value = AI.request;
    rq.addEventListener("input", () => (AI.request = rq.value));
    kids.push(h("label", { class: "form-label", for: "story-request" }, "Editing wishes ", h("span", { class: "muted", text: "(optional)" })), rq);
  }

  const counts = [["1", "1"], ["2", "2"], ["3", "3"], ["4", "4"]].slice(0, maxCount);
  kids.push(h("label", { class: "form-label", text: AI.mode === "story" ? "How many scenes" : "How many" }),
    choiceGroup(counts, AI.count, (v) => { AI.count = v; renderCreate(tok); }, { cls: "three", label: "How many" }));
  if (counts.length === 4) kids[kids.length - 1].style.gridTemplateColumns = "repeat(4, 1fr)";

  const n = Number(AI.count);
  kids.push(h("p", { class: "cost" }, icon("info"), videoish
    ? `Google charges for AI video by the second (about 8 seconds per clip). ${n} clip${n > 1 ? "s" : ""} can take a few minutes.`
    : "Google may charge a few cents per AI picture, depending on your plan."));
  if (videoish && !s.can_video) kids.push(h("div", { class: "note" }, icon("alert"), h("span", { text: "Your Google AI key can't make videos. Pictures still work." })));
  if (!videoish && !s.can_image) kids.push(h("div", { class: "note" }, icon("alert"), h("span", { text: "Your Google AI key can't make pictures." })));

  const go_ = h("button", { class: "btn btn-primary btn-block btn-lg", id: "create-btn", disabled: !!S.createJob },
    icon("sparkles"), AI.mode === "story" ? "Create my video" : AI.mode === "video" ? `Create ${n > 1 ? n + " clips" : "clip"}` : `Create ${n > 1 ? n + " pictures" : "picture"}`);
  go_.onclick = () => startCreate(go_, ta);
  kids.push(go_);
  body.replaceChildren(...kids);
}

async function startCreate(btn, ta) {
  const prompt = AI.prompt.trim();
  if (!prompt) { ta.focus(); toast("Please describe what you want first"); return; }
  btn.disabled = true;
  try {
    const d = await api("api/create", { method: "POST", json: {
      mode: AI.mode, prompt, format: AI.format, source: AI.source, count: Number(AI.count), request: AI.request.trim() } });
    S.lastCreateFormat = AI.format;
    setCreateJob(d.job);
    go("working", { id: d.job }, "replace");
  } catch (e) {
    toast(e.message, { bad: true });
    btn.disabled = false;
  }
}

function resultsBlock() {
  const r = S.createResult;
  const shapeCls = r.format === "16:9" ? " wide" : r.format === "1:1" ? " square" : "";
  const grid = h("div", { class: "results" });
  for (const f of r.files) {
    const media = h("div", { class: "media" });
    if (f.kind === "video") {
      const v = h("video", { src: f.url, controls: true, playsinline: true, preload: "metadata" });
      media.append(v);
    } else media.append(img(f.url, f.name));
    const save = h("button", { class: "btn" }, icon("download"), "Save to phone");
    save.onclick = () => saveFile(f);
    grid.append(h("div", { class: "result" + shapeCls }, media, save));
  }
  const usable = r.files.filter((f) => f.source);
  const use = usable.length ? h("button", { class: "btn btn-block", style: "margin-top:10px" }, icon("wand"), "Edit these into a video") : null;
  if (use) use.onclick = () => {
    for (const f of usable) {
      if (draft.sources.some((s) => s.id === f.source)) continue;
      draft.sources.push({ id: f.source, name: f.name, kind: f.kind, duration: null, thumb: f.kind === "image" ? f.url : null, _video: f.kind === "video" ? f.url : null, error: null });
    }
    draft.mode = "highlight";
    go("new", {}, R.screen === "home" ? "push" : "replace");
  };
  const n = r.files.length;
  const title = r.kind === "video" ? `Your AI video${n > 1 ? "s" : ""}` : `Your AI picture${n > 1 ? "s" : ""}`;
  const clear = h("button", { class: "btn btn-ghost btn-sm", onclick: () => { S.createResult = null; renderCreate(R.tok); } }, "Clear");
  return h("section", { style: "margin-bottom:22px" }, h("div", { class: "sec-head", style: "margin-top:4px" }, h("h2", { text: title }), clear), grid, use);
}

function saveFile(f) {
  const ext = (f.name.split(".").pop() || "").toLowerCase();
  const mime = f.kind === "video" ? "video/mp4" : ext === "png" ? "image/png" : ext === "webp" ? "image/webp" : "image/jpeg";
  if (APP) {
    try { window.Android.saveToGallery(f.url, f.name, mime); toast("Saving to your phone…"); } catch (e) { toast("Could not save.", { bad: true }); }
  } else {
    const a = h("a", { href: f.url, download: f.name });
    document.body.append(a); a.click(); a.remove();
  }
}

/* ------------------------------------------------------------------ */
/* Settings                                                            */
/* ------------------------------------------------------------------ */

async function settingsEnter(_p, tok) {
  renderSettings();
  await loadSettings();
  if (tok !== R.tok) return;
  renderSettings();
  if (!S.health) {
    try { S.health = await api("api/health"); } catch (_) { /* optional */ }
    if (tok === R.tok) renderAbout();
  }
}

function renderSettings() {
  const s = S.settings;
  const st = $("#ai-status");
  st.replaceChildren();
  if (s && s.key_hint) {
    for (const [ok, label] of [[s.can_text, "Understand anything you type"], [s.can_image, "Make AI pictures"], [s.can_video, "Make AI videos"]]) {
      st.append(h("li", { class: ok ? "" : "no-row" }, h("span", { class: ok ? "ok" : "no" }, icon(ok ? "check" : "x")),
        h("span", { text: label + (ok ? "" : " — not available with this key") })));
    }
    $("#key-input").placeholder = `Saved key ending in ${String(s.key_hint).replace(/[.…]/g, "")}`;
  } else {
    $("#key-input").placeholder = "Paste your key here";
  }
  $("#key-remove").hidden = !(s && s.key_hint);
  $("#key-save").textContent = s && s.key_hint ? "Save new key" : "Save key";

  const q = s ? s.quality : null, sp = s ? s.speed : null;
  $("#set-quality").replaceChildren(...choiceGroup([["720", "720p", "Smaller files"], ["1080", "1080p", "Sharper picture"]], q, (v) => saveSetting({ quality: v })).children);
  $("#set-speed").replaceChildren(...choiceGroup([["fast", "Fast", "Uses the phone's video chip"], ["best", "Best quality", "Slower, smaller files"]], sp, (v) => saveSetting({ speed: v })).children);
  bindChoiceGroup($("#set-quality"), (v) => saveSetting({ quality: v }));
  bindChoiceGroup($("#set-speed"), (v) => saveSetting({ speed: v }));
  if (!s) $$("#set-quality .choice, #set-speed .choice").forEach((b) => (b.disabled = true));
  renderAbout();
}

function bindChoiceGroup(wrap, onPick) {
  $$(".choice", wrap).forEach((b) => (b.onclick = () => {
    $$(".choice", wrap).forEach((x) => x.setAttribute("aria-checked", String(x === b)));
    onPick(b.dataset.value);
  }));
}

async function saveSetting(patch) {
  try {
    S.settings = await api("api/settings", { method: "POST", json: patch });
    toast("Saved");
  } catch (e) { toast(e.message, { bad: true }); }
  renderSettings();
}

function renderAbout() {
  const rows = [["App", "AI editor open source"]];
  if (APP && typeof window.Android.appVersion === "function") {
    try { rows.push(["Version", String(window.Android.appVersion())]); } catch (_) { /* ignore */ }
  }
  if (S.health && S.health.encoder) rows.push(["Video maker", S.health.encoder === "hardware" ? "Phone's video chip" : "Software (slower)"]);
  rows.push(["Running", APP ? "On this phone" : "In the browser"]);
  $("#about").replaceChildren(...rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]));
}

$("#key-eye").onclick = () => {
  const i = $("#key-input");
  i.type = i.type === "password" ? "text" : "password";
  $("#key-eye").setAttribute("aria-label", i.type === "password" ? "Show key" : "Hide key");
};
$("#key-link").addEventListener("click", (e) => {
  if (APP && typeof window.Android.openExternal === "function") { e.preventDefault(); window.Android.openExternal(e.currentTarget.href); }
});

async function saveKey(value) {
  const btn = $("#key-save"), err = $("#key-error");
  err.hidden = true;
  btn.disabled = true;
  btn.replaceChildren(h("div", { class: "spin" }), value ? "Checking key…" : "Removing…");
  try {
    S.settings = await api("api/settings", { method: "POST", json: { gemini_key: value } });
    $("#key-input").value = "";
    toast(value ? "Key saved" : "Key removed");
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
  btn.disabled = false;
  renderSettings();
}
$("#key-save").onclick = () => { const v = $("#key-input").value.trim(); if (v) saveKey(v); else $("#key-input").focus(); };
$("#key-remove").onclick = async () => {
  if (await confirmBox({ title: "Remove your Google AI key?", text: "AI pictures and videos will stop working until you add a key again.", ok: "Remove", danger: true })) saveKey("");
};

/* ------------------------------------------------------------------ */
/* Start                                                               */
/* ------------------------------------------------------------------ */

const SCREENS = {
  home: { enter: homeEnter },
  new: { enter: newEnter },
  working: { enter: workingEnter },
  project: { enter: projectEnter, leave: projectLeave },
  create: { enter: createEnter },
  settings: { enter: settingsEnter },
};

(function boot() {
  let st = history.state && history.state.screen ? history.state : null;
  if (!st) {
    const first = parseHash();
    history.replaceState({ screen: "home", params: {}, depth: 0 }, "", "#/home");
    if (first.screen !== "home") {
      // Opened straight on an inner screen: keep Home underneath so Back goes there.
      history.pushState({ screen: first.screen, params: first.params, depth: 1 }, "", hashFor(first.screen, first.params));
    }
    st = history.state;
  }
  show(st, false);
  loadSettings();
  api("api/caption-styles").then((d) => { S.styles = d; if (!draft.options.caption_style) draft.options.caption_style = d.default; }).catch(() => {});
})();

if ("serviceWorker" in navigator && !APP) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}
