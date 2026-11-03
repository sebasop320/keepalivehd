"use strict";

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 5000;
let timer = null;
let schedDirty = false;
let busy = false;

async function api(path, body) {
  const opts = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-Requested-With"] = "keepalivehd";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (_) { /* ignore */ }
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Signed out"); }
  if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
  return data;
}

function toast(msg, bad) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("bad", !!bad);
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 3500);
}

function ago(sec) {
  if (sec === null || sec === undefined) return "never";
  if (sec < 90) return sec + " s ago";
  if (sec < 5400) return Math.round(sec / 60) + " min ago";
  if (sec < 172800) return Math.round(sec / 3600) + " h ago";
  return Math.round(sec / 86400) + " d ago";
}
const yesno = (v) => (v === true ? "yes" : v === false ? "no" : "unknown");
const fmtTime = (ts) => new Date(ts * 1000).toLocaleString();

function showLogin() {
  stop();
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  $("lg-pass").value = "";
}
function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  start();
}

const STATE_TEXT = {
  active: "Active", idle: "Inactive", waiting: "Waiting for drive", paused: "Paused (temperature)",
};

function render(st) {
  const ka = st.keepalive, d = st.disk, sm = st.smart;
  const s = $("ka-state");
  s.textContent = STATE_TEXT[ka.state] || ka.state;
  s.className = "state " + ka.state;
  $("ka-reason").textContent = ka.reason;
  $("ka-next").textContent = ka.next_read_in !== null ? "Next read in " + ka.next_read_in + " s (every " + ka.interval_seconds + " s)" : "";
  document.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("sel", b.dataset.mode === st.mode));

  $("d-conn").textContent = yesno(d.connected);
  $("d-mount").textContent = d.mounted ? "mounted at " + d.mountpoint : "not mounted";
  $("d-dev").textContent = d.device ? d.device + (d.usb_id ? " (USB " + d.usb_id + ")" : "") : "—";
  $("d-model").textContent = d.model || "—";
  $("d-problem").textContent = d.ready ? "" : (d.problem || "");

  $("s-temp").textContent = sm.temperature !== null ? sm.temperature + " °C" : "—";
  const lv = $("s-level");
  if (sm.temperature_level) {
    lv.className = "badge " + sm.temperature_level;
    lv.textContent = { ok: "normal", warn: "warm", critical: "critical", protect: "protect" }[sm.temperature_level];
  } else {
    lv.className = "badge hidden";
  }
  let power = sm.power_state;
  if (power === "unknown" && !sm.polled) power = ka.state === "idle" ? "not polled (idle period)" : "unknown";
  $("s-power").textContent = power;
  $("s-health").textContent = sm.health || "—";
  $("s-age").textContent = sm.polled ? ago(sm.polled_age) : "—";

  $("k-last").textContent = ka.last_ok ? ago(ka.last_ok_age) : "never";
  $("k-when").textContent = ka.last_ok ? fmtTime(ka.last_ok) + (ka.last_read_ms ? " · " + Math.round(ka.last_read_ms) + " ms" : "") : "";
  $("k-err").textContent = ka.consecutive_failures ? ka.consecutive_failures + " consecutive failed read(s)" : "";

  if (!schedDirty) {
    $("sc-start").value = st.schedule.start;
    $("sc-end").value = st.schedule.end;
    $("sc-int").value = st.schedule.interval_seconds;
  }
  $("tz").textContent = st.timezone;
  $("clock").textContent = st.server_time.replace("T", " ").slice(0, 19) + " " + st.timezone;
  $("foot").textContent = "KeepAliveHD " + st.version + " · uptime " + Math.round(st.uptime_seconds / 60) + " min";
}

async function renderLogs() {
  const r = await api("/api/logs?limit=120&level=" + encodeURIComponent($("log-level").value));
  const ul = $("log");
  ul.replaceChildren();
  if (!r.logs.length) {
    const li = document.createElement("li");
    li.className = "log-empty";
    li.textContent = "Nothing logged at this level.";
    ul.appendChild(li);
    return;
  }
  for (const e of r.logs) {
    const li = document.createElement("li");
    const t = document.createElement("span"); t.className = "muted"; t.textContent = fmtTime(e.ts);
    const l = document.createElement("span"); l.className = "lv " + e.level; l.textContent = e.level.toUpperCase();
    const m = document.createElement("span"); m.textContent = e.msg;
    li.append(t, l, m);
    ul.appendChild(li);
  }
}

async function refresh() {
  if (busy) return;
  try {
    render(await api("/api/status"));
    await renderLogs();
  } catch (e) {
    if (e.message !== "Signed out") $("foot").textContent = "Connection problem: " + e.message;
  }
}
function start() { stop(); refresh(); timer = setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS); }
function stop() { if (timer) clearInterval(timer); timer = null; }
document.addEventListener("visibilitychange", () => { if (!document.hidden && timer) refresh(); });

async function guarded(fn) {
  busy = true;
  try { await fn(); } catch (e) { toast(e.message, true); } finally { busy = false; }
}

document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => guarded(async () => {
  render(await api("/api/mode", { mode: b.dataset.mode }));
  toast("Mode set to " + b.dataset.mode.toUpperCase());
  await renderLogs();
})));

$("sched-form").addEventListener("input", () => { schedDirty = true; });
$("sched-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  guarded(async () => {
    await api("/api/schedule", {
      start: $("sc-start").value, end: $("sc-end").value,
      interval_seconds: parseInt($("sc-int").value, 10),
    });
    schedDirty = false;
    toast("Schedule saved");
    render(await api("/api/status"));
    await renderLogs();
  });
});

$("standby").addEventListener("click", () => {
  if (!confirm("Switch to OFF and send the drive to standby now?\n\nIt will wake on its own when something reads it. Choose AUTO or ON to resume keepalive.")) return;
  guarded(async () => {
    $("standby").disabled = true;
    try { render(await api("/api/standby", {})); toast("Standby command sent; mode is OFF"); await renderLogs(); }
    finally { $("standby").disabled = false; }
  });
});

$("probe").addEventListener("click", () => guarded(async () => {
  $("probe").disabled = true;
  try { render(await api("/api/probe", {})); toast("Drive checked"); }
  finally { $("probe").disabled = false; }
}));

$("log-level").addEventListener("change", () => guarded(renderLogs));

$("logout").addEventListener("click", async () => {
  try { await api("/api/logout", {}); } catch (_) { /* ignore */ }
  showLogin();
});

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("lg-err").textContent = "";
  try {
    await api("/api/login", { username: $("lg-user").value, password: $("lg-pass").value });
    showApp();
  } catch (e) {
    $("lg-err").textContent = e.message;
  }
});

(async function init() {
  try {
    const s = await api("/api/session");
    if (s.authenticated) showApp(); else showLogin();
  } catch (_) { showLogin(); }
})();
