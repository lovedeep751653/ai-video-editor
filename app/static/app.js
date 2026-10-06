const $ = (id) => document.getElementById(id);
const files = [];
let music = null;
const options = { format: "auto", length: "auto", style: "auto", look: "auto", transitions: "auto", slowmo: "auto",
  captions: "auto", caption_lang: "auto", caption_pos: "bottom", caption_style: "" };
const ai = { mode: "image", aformat: "9:16", source: "pictures", count: "1" };
let settings = { ai_ready: false };
let currentTab = "edit";
let pollTimer = null;
let afterLogin = null;

const SECTIONS = ["edit", "create", "settings", "login", "working", "result", "error"];
function show(section) {
  for (const id of SECTIONS) $(id).hidden = id !== section;
  $("tabs").hidden = !["edit", "create"].includes(section);
  window.scrollTo(0, 0);
}
function showTab(tab) {
  currentTab = tab;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  show(tab);
}
document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

// ---------- talking to the server ----------
async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (res.status === 401 && data.login) {
    show("login");
    return new Promise((resolve, reject) => { afterLogin = () => api(url, opts).then(resolve, reject); });
  }
  if (!res.ok) throw new Error(data.detail || "Something went wrong. Please try again.");
  return data;
}

$("code-btn").onclick = async () => {
  const form = new FormData();
  form.append("code", $("code-input").value);
  try {
    await api("api/login", { method: "POST", body: form });
    $("login-error").textContent = "";
    showTab(currentTab);
    if (afterLogin) { const f = afterLogin; afterLogin = null; f(); }
    loadSettings();
  } catch (e) { $("login-error").textContent = e.message; }
};

// ---------- edit tab ----------
function renderList() {
  const list = $("file-list");
  list.innerHTML = "";
  files.forEach((f, i) => {
    const li = document.createElement("li");
    const url = URL.createObjectURL(f);
    const isVideo = f.type.startsWith("video") || /\.(mp4|mov|webm|mkv|3gp)$/i.test(f.name);
    const el = document.createElement(isVideo ? "video" : "img");
    el.src = isVideo ? url + "#t=0.5" : url;
    if (isVideo) { el.muted = true; el.preload = "metadata"; el.playsInline = true; }
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = isVideo ? "Video" : "Photo";
    const rm = document.createElement("button");
    rm.className = "remove";
    rm.setAttribute("aria-label", "Remove");
    rm.textContent = "×";
    rm.onclick = () => { files.splice(i, 1); renderList(); };
    li.append(el, tag, rm);
    list.append(li);
  });
  $("empty-hint").hidden = files.length > 0;
  $("make-btn").disabled = files.length === 0;
}

$("file-input").addEventListener("change", (e) => {
  for (const f of e.target.files) files.push(f);
  e.target.value = "";
  renderList();
});
$("music-input").addEventListener("change", (e) => {
  music = e.target.files[0] || null;
  e.target.value = "";
  renderMusic();
});
$("music-remove").onclick = () => { music = null; renderMusic(); };
function renderMusic() {
  $("music-label").textContent = music ? `Music: ${music.name}` : "Add music (optional)";
  $("music-remove").hidden = !music;
}

document.querySelectorAll(".chips").forEach((group) => {
  group.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    group.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
    const target = group.dataset.group === "ai" ? ai : options;
    target[group.dataset.name] = btn.dataset.value;
    if (group.dataset.group === "ai") updateCreateForm();
    if (group.dataset.name === "caption_lang") loadStyles();
  });
});

function setProgress(title, frac, stage) {
  $("work-title").textContent = title;
  const pct = Math.round(frac * 100);
  $("bar-fill").style.width = pct + "%";
  $("work-pct").textContent = pct + "%";
  $("work-stage").textContent = stage || "";
}

function fail(msg) {
  clearTimeout(pollTimer);
  $("error-text").textContent = msg;
  show("error");
}

function upload(url, form, workingTitle) {
  show("working");
  setProgress("Uploading…", 0, "Sending");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", url);
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) setProgress("Uploading…", e.loaded / e.total, "Sending your files");
  };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status === 401 && data.login) { show("login"); afterLogin = () => upload(url, form, workingTitle); return; }
    if (xhr.status !== 200) return fail(data.detail || "Upload failed. Please try again.");
    poll(data.id, workingTitle);
  };
  xhr.onerror = () => fail("Upload failed. Check your internet connection and try again.");
  xhr.send(form);
}

$("make-btn").addEventListener("click", () => {
  const form = new FormData();
  files.forEach((f) => form.append("files", f, f.name));
  if (music) form.append("music", music, music.name);
  for (const [k, v] of Object.entries(options)) if (v !== "") form.append(k, v);
  form.append("title", $("title").value);
  form.append("request", $("request").value);
  upload("api/jobs", form, "Editing your video…");
});

async function poll(id, workingTitle) {
  try {
    const job = await api(`api/jobs/${id}`);
    if (job.state === "failed") return fail(job.error);
    if (job.state === "done") return showResult(id, job);
    setProgress(job.state === "waiting" ? "Waiting to start…" : workingTitle, job.progress, job.stage);
  } catch (e) {
    if (/not found/i.test(e.message)) return fail(e.message);
    // Brief network hiccup on mobile: keep trying.
  }
  pollTimer = setTimeout(() => poll(id, workingTitle), 1500);
}

// ---------- results ----------
const fmt = (s) => s.toFixed(1).replace(/\.0$/, "") + "s";

function showResult(id, job) {
  const r = job.result || {};
  const hasFinal = job.kind === "edit" || job.kind === "story";
  $("final-box").hidden = !hasFinal;
  $("result-title").textContent = hasFinal ? "Your video is ready"
    : (r.files || []).length > 1
      ? (job.kind === "video" ? "Your AI videos are ready" : "Your AI pictures are ready")
      : (job.kind === "video" ? "Your AI video is ready" : "Your AI picture is ready");
  if (hasFinal) {
    $("player").src = `api/jobs/${id}/video`;
    $("player").poster = `api/jobs/${id}/thumb`;
    $("download-btn").href = `api/jobs/${id}/download`;
  } else {
    $("player").removeAttribute("src");
  }
  renderGallery(id, r.files || [], hasFinal);
  const notes = $("notes");
  notes.innerHTML = "";
  const lines = [];
  if (r.understood && r.understood.length) lines.push("You asked for: " + r.understood.join(", "));
  if (r.format) {
    const shapeName = { "9:16": "Tall 9:16", "16:9": "Wide 16:9", "1:1": "Square 1:1" }[r.format];
    lines.push(`${shapeName}, ${fmt(r.total)} long, ${r.clips.length} parts`);
  }
  lines.push(...(r.notes || []), ...(r.skipped || []).map((s) => "Skipped " + s));
  if (r.shots) lines.push(...r.shots.map((s, i) => `Shot ${i + 1}: ${s}`));
  for (const n of lines) {
    const li = document.createElement("li");
    li.textContent = n;
    notes.append(li);
  }
  const list = $("plan-list");
  list.innerHTML = "";
  for (const c of r.clips || []) {
    const li = document.createElement("li");
    const name = c.name.replace(/^\d\d_/, "");
    const b = document.createElement("b");
    b.textContent = c.kind === "image" ? `Photo: ${name}` : `${name} from ${fmt(c.start)}`;
    const small = document.createElement("small");
    small.textContent = " " + c.reasons.join(", ");
    li.append(b, document.createElement("br"), small);
    list.append(li);
  }
  $("plan-box").hidden = lines.length === 0 && !(r.clips || []).length;
  show("result");
}

function renderGallery(id, items, compact) {
  const g = $("gallery");
  g.innerHTML = "";
  for (const f of items) {
    const li = document.createElement("li");
    const url = `api/jobs/${id}/files/${encodeURIComponent(f.name)}`;
    const el = document.createElement(f.kind === "video" ? "video" : "img");
    el.src = url;
    if (f.kind === "video") { el.controls = true; el.playsInline = true; el.preload = "metadata"; }
    const row = document.createElement("div");
    row.className = "row";
    const dl = document.createElement("a");
    dl.href = url + "?download=true";
    dl.textContent = "Download";
    dl.setAttribute("download", f.name);
    const use = document.createElement("button");
    use.textContent = "Use in edit";
    use.onclick = async () => {
      use.textContent = "Adding…";
      const blob = await (await fetch(url)).blob();
      files.push(new File([blob], f.name, { type: blob.type || (f.kind === "video" ? "video/mp4" : "image/png") }));
      renderList();
      showTab("edit");
    };
    row.append(dl, use);
    li.append(el, row);
    g.append(li);
  }
  g.classList.toggle("compact", compact);
}

$("again-btn").addEventListener("click", () => {
  if (currentTab === "edit") { files.length = 0; renderList(); }
  $("player").removeAttribute("src");
  showTab(currentTab);
});
$("retry-btn").addEventListener("click", () => showTab(currentTab));

// ---------- create with AI ----------
function updateCreateForm() {
  const video = ai.mode === "video" || (ai.mode === "story" && ai.source === "clips");
  $("story-opts").hidden = ai.mode !== "story";
  // Google's video AI makes tall or wide clips only, and at most 3 at a time here.
  document.querySelectorAll("[data-not-video]").forEach((b) => (b.hidden = video));
  if (video && ai.aformat === "1:1") document.querySelector('[data-name=aformat] [data-value="9:16"]').click();
  if (video && ai.count === "4") document.querySelector('[data-name=count] [data-value="3"]').click();
  const n = Number(ai.count);
  $("cost-hint").textContent = video
    ? `Google charges for AI video by the second (about 8 seconds per clip). ${n} clip${n > 1 ? "s" : ""} can take a few minutes.`
    : `Google may charge a few cents per AI picture, depending on your plan.`;
  $("create-btn").textContent = ai.mode === "story" ? "Create my video" : "Create";
}

$("create-btn").onclick = () => {
  const prompt = $("prompt").value.trim();
  if (!prompt) { $("prompt").focus(); $("prompt").placeholder = "Please describe what you want first"; return; }
  const form = new FormData();
  form.append("mode", ai.mode);
  form.append("prompt", prompt);
  form.append("format", ai.aformat);
  form.append("source", ai.source);
  form.append("count", ai.count);
  form.append("request", $("story-request").value);
  upload("api/create", form, ai.mode === "image" ? "Creating your picture…" : "Creating your video…");
};

// ---------- settings ----------
async function loadSettings() {
  try {
    settings = await api("api/settings");
  } catch (_) { return; }
  $("ai-off").hidden = settings.ai_ready;
  $("ai-on").hidden = !settings.ai_ready;
  $("request-hint").textContent = settings.can_text
    ? "Google AI will read your request."
    : "Understands common words like cinematic, vertical, 30 seconds, slow motion, title \"…\". Add a Google AI key in Settings for full understanding.";
  const s = $("ai-status");
  s.innerHTML = "";
  if (settings.key_hint) {
    const rows = [
      [settings.can_text, "Understand typed requests"],
      [settings.can_image, "Make AI pictures"],
      [settings.can_video, "Make AI videos"],
    ];
    for (const [ok, label] of rows) {
      const li = document.createElement("li");
      li.textContent = `${ok ? "✅" : "❌"} ${label}${ok ? "" : " (not available for this key)"}`;
      s.append(li);
    }
    $("key-input").placeholder = `Saved key ending ${settings.key_hint.replace("…", "")}`;
  } else {
    $("key-input").placeholder = "Paste your key here";
  }
  $("key-remove").hidden = !settings.key_hint;
  updateCreateForm();
}

async function saveKey(value) {
  const form = new FormData();
  form.append("gemini_key", value);
  $("key-save").textContent = "Checking key…";
  try {
    await api("api/settings", { method: "POST", body: form });
    $("key-input").value = "";
    await loadSettings();
  } catch (e) {
    const li = document.createElement("li");
    li.className = "error";
    li.textContent = e.message;
    $("ai-status").replaceChildren(li);
  }
  $("key-save").textContent = "Save key";
}
$("key-save").onclick = () => $("key-input").value.trim() && saveKey($("key-input").value.trim());
$("key-remove").onclick = () => saveKey("");
$("settings-btn").onclick = () => show("settings");
document.querySelectorAll("[data-open-settings]").forEach((b) => (b.onclick = () => show("settings")));
document.querySelectorAll("[data-back]").forEach((b) => (b.onclick = () => { loadSettings(); showTab(currentTab); }));

// ---------- caption styles (real previews drawn by the server) ----------
let stylesData = null;
async function loadStyles() {
  try {
    if (!stylesData) stylesData = await api("api/caption-styles");
  } catch (_) { return; }
  if (!options.caption_style) options.caption_style = stylesData.default;
  const lang = options.caption_lang === "auto" ? "en" : options.caption_lang;
  $("style-count").textContent = `(${stylesData.styles.length} styles)`;
  const grid = $("style-grid");
  grid.innerHTML = "";
  for (const st of stylesData.styles) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.classList.toggle("on", st.id === options.caption_style);
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = st.name;
    img.src = `api/caption-preview/${st.id}?lang=${lang}`;
    const name = document.createElement("span");
    name.textContent = st.name;
    b.append(img, name);
    b.onclick = () => {
      options.caption_style = st.id;
      grid.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    };
    li.append(b);
    grid.append(li);
  }
}
$("options").addEventListener("toggle", () => { if ($("options").open) loadStyles(); });

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}

renderList();
renderMusic();
loadSettings();
