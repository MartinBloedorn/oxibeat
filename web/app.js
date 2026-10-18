"use strict";

const WINDOW_SECONDS = 6;     // time span shown across the graph
const DEFAULT_RATE = 50;      // samples/s until we have measured the real rate
const TARGET_DELAY = 0.4;     // seconds of samples kept queued; BLE packets can arrive ~0.5 s apart
const MAX_BACKLOG = 1.0;      // seconds; beyond this the queue is cut back (e.g. after a hidden tab)
const VALUE_MAX = 127;

const $ = (id) => document.getElementById(id);
const canvas = $("wave");
const ctx = canvas.getContext("2d");
const playBtn = $("play");
const linkEl = $("link");
const linkLabel = $("link-label");
const overlay = $("overlay");
const beatEl = $("beat");
const detectionEl = $("detection");
const legendEl = $("legend");

// how each kind of mark is drawn; beats are the kinds that flash the beat light
const MARKS = {
  beat:      { label: "Beat (device)",  color: "mark-beat",      dash: [],     beat: true },
  systolic:  { label: "Systolic peak",  color: "mark-beat",      dash: [],     beat: true },
  notch:     { label: "Dicrotic notch", color: "mark-notch",     dash: [6, 4] },
  diastolic: { label: "Diastolic peak", color: "mark-diastolic", dash: [2, 3] },
};

let ws = null;
let serverUp = false;
let running = true;
let device = { state: "scanning", name: null };
let detection = null;
let detectors = [];

// --- sample playout ---------------------------------------------------------
// Waveform packets arrive in bursts; they are queued and played out at the
// measured sample rate so the graph scrolls smoothly.

const queue = [];             // pending {v, sn}; sn = the server's sample number
const history = [];           // displayed {v, sn, n}, oldest first; n = local running sample number
const marks = new Map();      // server sample number -> kind; marks can arrive after their sample
let lastSn = -1;
let shown = 0;                // count of samples moved to history
let acc = 0;                  // fractional progress towards the next sample
let rate = DEFAULT_RATE;      // samples/s; it sets the x scale, so it only changes on a clear shift
let speed = 1;                // playout speed relative to `rate`, smoothed
let buffering = true;         // waiting for the queue to fill before (re)starting playback
const arrivals = [];          // [time ms, sample count] over the last few seconds

function onWave(n0, samples) {
  if (n0 <= lastSn) marks.clear();                  // server restarted its numbering
  lastSn = n0 + samples.length - 1;
  const now = performance.now();
  // after a pause in the stream (device off, reconnect) measure the rate afresh;
  // counting the silence would read as a slow rate and rescale the x axis
  if (arrivals.length && now - arrivals[arrivals.length - 1][0] > 1000) arrivals.length = 0;
  arrivals.push([now, samples.length]);
  while (arrivals.length && now - arrivals[0][0] > 5000) arrivals.shift();
  const span = (now - arrivals[0][0]) / 1000;
  if (span > 3) {
    const count = arrivals.reduce((s, a) => s + a[1], 0) - arrivals[0][1];
    const estimate = count / span;
    if (Math.abs(estimate - rate) / rate > 0.15) rate = Math.min(250, Math.max(10, Math.round(estimate)));
  }
  samples.forEach((v, i) => queue.push({ v, sn: n0 + i }));
  const max = Math.round(rate * MAX_BACKLOG);
  if (queue.length > max) queue.splice(0, queue.length - Math.round(rate * TARGET_DELAY));
}

function advance(dt) {
  if (!running) return;
  const target = rate * TARGET_DELAY;
  // when the queue has run dry (stream restarted, resume after pause), wait until it
  // holds the full target delay again; starting at once stutters until it has refilled
  if (queue.length === 0) buffering = true;
  if (buffering) {
    if (queue.length < target) return;
    buffering = false;
    speed = 1;
  }
  // gently nudge the playout speed so the queue hovers around the target delay;
  // smoothed so packet bursts don't show up as jerks in the scrolling
  const wanted = Math.min(1.25, Math.max(0.8, 1 + 0.25 * (queue.length - target) / target));
  speed += (wanted - speed) * Math.min(1, dt * 1.5);
  acc += rate * speed * dt;
  while (acc >= 1 && queue.length) {
    const s = queue.shift();
    history.push({ v: s.v, sn: s.sn, n: shown++ });
    if (MARKS[marks.get(s.sn)]?.beat) flashBeat();
    acc -= 1;
  }
  if (queue.length === 0) acc = Math.min(acc, 1);
  const keep = Math.ceil(rate * WINDOW_SECONDS) + 2;
  if (history.length > keep * 2) {
    history.splice(0, history.length - keep);
    for (const sn of marks.keys()) if (sn < history[0].sn) marks.delete(sn);
  }
}

function onMarks(list) {
  for (const [sn, kind] of list) marks.set(sn, kind);
}

function flashBeat() {
  beatEl.classList.add("on");
  requestAnimationFrame(() => requestAnimationFrame(() => beatEl.classList.remove("on")));
}

// --- drawing ---------------------------------------------------------------

const colors = {};
function readColors() {
  const css = getComputedStyle(document.documentElement);
  for (const k of ["grid", "wave", "mark-beat", "mark-notch", "mark-diastolic"]) {
    colors[k] = css.getPropertyValue(`--${k}`).trim();
  }
}
readColors();

// light/dark toggle; the page starts dark (see the script in index.html's head)
const themeBtn = $("theme");
function renderTheme() {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  themeBtn.title = themeBtn.ariaLabel = `Switch to ${next} mode`;
}
themeBtn.addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("oxibeat.theme", theme);
  } catch {
    // not remembered without storage
  }
  readColors();
  renderTheme();
});
renderTheme();

function resize() {
  const dpr = window.devicePixelRatio || 1;
  const { width, height } = canvas.getBoundingClientRect();
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
new ResizeObserver(resize).observe(canvas);

function draw() {
  const { width: w, height: h } = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, w, h);

  const pad = 12;
  const y = (v) => h - pad - (v / VALUE_MAX) * (h - 2 * pad);
  const px = w / (rate * WINDOW_SECONDS);           // pixels per sample
  // newest sample sits at the right edge, sliding left as `acc` grows
  const x = (n) => w - (shown - 1 - n + Math.min(acc, 1)) * px;

  // horizontal grid
  ctx.strokeStyle = colors.grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i <= 4; i++) {
    const gy = Math.round(y((VALUE_MAX * i) / 4)) + 0.5;
    ctx.moveTo(0, gy);
    ctx.lineTo(w, gy);
  }
  // one vertical line per second, scrolling with the data
  const step = Math.max(1, Math.round(rate));
  const first = history.length ? history[0].n : 0;
  for (let n = Math.ceil(first / step) * step; n < shown; n += step) {
    const gx = Math.round(x(n)) + 0.5;
    ctx.moveTo(gx, 0);
    ctx.lineTo(gx, h);
  }
  ctx.stroke();

  if (history.length < 2) return;

  // detected marks: a vertical line per mark, styled by kind
  ctx.lineWidth = 2;
  ctx.globalAlpha = 0.7;
  for (const s of history) {
    const style = MARKS[marks.get(s.sn)];
    if (!style) continue;
    const mx = x(s.n);
    ctx.strokeStyle = colors[style.color];
    ctx.setLineDash(style.dash);
    ctx.beginPath();
    ctx.moveTo(mx, 0);
    ctx.lineTo(mx, h);
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;

  // waveform
  ctx.strokeStyle = colors.wave;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  history.forEach((s, i) => (i ? ctx.lineTo(x(s.n), y(s.v)) : ctx.moveTo(x(s.n), y(s.v))));
  ctx.stroke();
}

let last = performance.now();
function frame(now) {
  advance(Math.min(0.1, (now - last) / 1000));
  last = now;
  draw();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// --- transport & status ----------------------------------------------------

function render() {
  const state = serverUp ? device.state : "offline";
  linkEl.dataset.state = state;
  linkLabel.textContent = {
    offline: "Server offline, reconnecting…",
    scanning: "Searching for oximeter…",
    connecting: `Connecting to ${device.name}…`,
    connected: `Connected · ${device.name}`,
  }[state];

  playBtn.disabled = !serverUp;
  detectionEl.disabled = !serverUp;
  if (!serverUp) {
    renderMidi();
    renderOsc();
  }
  playBtn.classList.toggle("paused", !running);
  playBtn.setAttribute("aria-label", running ? "Pause" : "Play");

  if (state !== "connected") {
    overlay.hidden = false;
    overlay.textContent = state === "offline" ? "No connection to the oxibeat server" : "Waiting for oximeter";
  } else {
    overlay.hidden = running;
    overlay.textContent = "Paused";
  }
  if (state !== "connected") setVitals(null);
}

let pulseRate = 0;  // latest rate from the device, for the note length hint

function setVitals(v) {
  pulseRate = v?.pr ?? 0;
  if (midi) renderMidi();
  // the device reports 0 while it has no reading (e.g. finger removed)
  $("spo2").textContent = v && v.spo2 ? v.spo2 : "–";
  $("pr").textContent = v && v.pr ? v.pr : "–";
  $("pi").textContent = v && v.pi ? v.pi.toFixed(1) : "–";
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function setRunning(value) {
  send({ type: "set_running", running: value });
}

playBtn.addEventListener("click", () => setRunning(!running));
detectionEl.addEventListener("change", () => send({ type: "set_detection", detection: detectionEl.value }));
// Space is play/pause anywhere except where it types or opens a list; on a focused
// checkbox, button or slider it would otherwise toggle that control instead
const takesSpace = (el) => el.matches?.('input[type="text"], input[type="number"], select, textarea');
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || takesSpace(e.target)) return;
  e.preventDefault();
  if (!e.repeat) setRunning(!running);
});
// buttons and checkboxes act on keyup
document.addEventListener("keyup", (e) => {
  if (e.code === "Space" && !takesSpace(e.target)) e.preventDefault();
});

function onStatus(msg) {
  if (msg.running && !running) queue.length = 0;   // don't replay stale samples on resume
  running = msg.running;
  device = msg.device;
  if (msg.detection !== detection) marks.clear();  // marks from the previous detector no longer apply
  detection = msg.detection;
  detectors = msg.detectors;
  midi = msg.midi;
  osc = msg.osc;
  syncSettings(msg);
  renderDetection();
  renderMidi();
  renderOsc();
  render();
}

function renderDetection() {
  if (detectionEl.options.length !== detectors.length) {
    detectionEl.replaceChildren(...detectors.map((d) => new Option(d.label, d.id)));
  }
  detectionEl.value = detection;
  const kinds = detectors.find((d) => d.id === detection)?.kinds ?? [];
  legendEl.replaceChildren(...kinds.map((kind) => {
    const item = document.createElement("span");
    item.className = `legend-item kind-${kind}`;
    item.innerHTML = '<svg width="10" height="16" aria-hidden="true"><line x1="5" y1="0" x2="5" y2="16"/></svg>';
    item.append(MARKS[kind].label);
    return item;
  }));
}

// --- MIDI output -----------------------------------------------------------

const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const noteName = (n) => `${NOTE_NAMES[n % 12]}${Math.floor(n / 12) - 1}`;  // 60 = C4
const midiEls = {
  port: $("midi-port"),
  refresh: $("midi-refresh"),
  connect: $("midi-connect"),
  status: $("midi-status"),
  channel: $("midi-channel"),
  ccEnabled: $("cc-enabled"),
  ccNumber: $("cc-number"),
  notesHint: $("notes-hint"),
  velocityFixed: $("velocity-fixed"),
  velocityValue: $("velocity-value"),
  length: $("note-length"),
  notes: [...document.querySelectorAll(".note-row[data-kind]")],
  clockEnabled: $("clock-enabled"),
  clockTransport: $("clock-transport"),
  clockFraction: $("clock-fraction"),
  clockSensitivity: $("clock-sensitivity"),
  clockHint: $("clock-hint"),
};
let midi = null;

for (let ch = 1; ch <= 16; ch++) midiEls.channel.append(new Option(ch, ch));

// don't overwrite a field the user is typing in
function setValue(el, value) {
  if (document.activeElement !== el) el.value = value;
}

function renderMidi() {
  const live = serverUp && midi;
  const connected = live && midi.port !== null;
  for (const el of document.querySelectorAll(".menu-section [id^=midi-], .menu-section [id^=cc-], .note-row input")) {
    el.disabled = !live;
  }
  if (!midi) return;

  const ports = midi.port && !midi.ports.includes(midi.port) ? [midi.port, ...midi.ports] : midi.ports;
  const selected = midiEls.port.value;
  midiEls.port.replaceChildren(...(ports.length ? ports.map((p) => new Option(p, p)) : [new Option("No MIDI outputs", "")]));
  midiEls.port.value = connected ? midi.port : ports.includes(selected) ? selected : ports[0] ?? "";
  midiEls.port.disabled = !live || connected || !ports.length;
  midiEls.refresh.disabled = !live || connected;
  midiEls.connect.textContent = connected ? "Disconnect" : "Connect";
  midiEls.connect.classList.toggle("active", connected);
  midiEls.connect.disabled = !live || (!connected && !ports.length);
  midiEls.status.textContent = midi.error ?? (connected ? `Sending on channel ${midi.channel}` : "Not connected");
  midiEls.status.classList.toggle("error", Boolean(midi.error));

  setValue(midiEls.channel, midi.channel);
  midiEls.ccEnabled.checked = midi.cc.enabled;
  setValue(midiEls.ccNumber, midi.cc.number);

  // marks the current detector doesn't produce are shown but unavailable
  const kinds = detectors.find((d) => d.id === detection)?.kinds ?? [];
  const available = (kind) => kinds.includes(kind) || (kind === "systolic" && kinds.includes("beat"));
  for (const row of midiEls.notes) {
    const kind = row.dataset.kind;
    const [check, number] = row.querySelectorAll("input");
    const ok = available(kind);
    row.classList.toggle("unavailable", !ok);
    check.disabled = number.disabled = !live || !ok;
    check.checked = midi.notes[kind].enabled;
    setValue(number, midi.notes[kind].note);
    row.querySelector(".note-name").textContent = noteName(midi.notes[kind].note);
  }
  midiEls.velocityFixed.checked = midi.velocity.fixed;
  setValue(midiEls.velocityValue, midi.velocity.value);
  midiEls.velocityValue.disabled = !live || !midi.velocity.fixed;
  midiEls.length.value = midi.length;
  midiEls.length.disabled = !live;

  // same rule as the server: an implausible or missing rate counts as 120 bpm
  const bpm = pulseRate >= 20 && pulseRate <= 300 ? pulseRate : 120;
  const beats = { "1/4": 1, "1/8": 0.5, "1/16": 0.25, "1/32": 0.125 }[midi.length];
  const parts = [`${midi.length} at ${bpm} bpm = ${Math.round((60000 / bpm) * beats)} ms`];
  if (!midi.velocity.fixed) parts.push(`velocity ${Math.min(127, bpm)} (the pulse rate)`);
  if (kinds.includes("beat")) parts.push("the device's beat flag plays the systolic note");
  midiEls.notesHint.textContent = `${parts.join("; ")}.`;

  const clock = midi.clock;
  midiEls.clockEnabled.checked = clock.enabled;
  midiEls.clockTransport.checked = clock.transport;
  midiEls.clockFraction.value = clock.fraction;
  setValue(midiEls.clockSensitivity, clock.sensitivity);
  for (const el of [midiEls.clockEnabled, midiEls.clockTransport, midiEls.clockFraction, midiEls.clockSensitivity]) {
    el.disabled = !live;
  }
  renderClockHint(bpm);
}

// same mapping as backend/clock.py's time_constant(): 16 heartbeats at 0, 1.5 at 100
const clockTau = (sensitivity) => 16 * (1.5 / 16) ** (sensitivity / 100);

function renderClockHint(bpm) {
  const clock = midi.clock;
  const perBeat = clock.fraction === "1/2" ? 2 : 1;
  const tau = clockTau(intValue(midiEls.clockSensitivity));
  const parts = [
    `${bpm * perBeat} bpm clock at a ${bpm} bpm pulse`,
    `follows tempo changes within ~${tau < 3 ? tau.toFixed(1) : Math.round(tau)} heartbeats`,
    clock.transport
      ? "Start on a heartbeat once locked, Stop 5 s after the pulse is lost"
      : "keeps the last tempo without a pulse",
  ];
  midiEls.clockHint.textContent = `${parts.join("; ")}.`;
}

const intValue = (el) => Number.parseInt(el.value, 10);

midiEls.refresh.addEventListener("click", () => send({ type: "midi_refresh" }));
midiEls.connect.addEventListener("click", () => {
  if (midi.port !== null) {
    send({ type: "midi_disconnect" });
    saveSettings({ midiPort: null });
  } else if (midiEls.port.value) {
    send({ type: "midi_connect", port: midiEls.port.value });
    saveSettings({ midiPort: midiEls.port.value });
  }
});
midiEls.channel.addEventListener("change", () => send({ type: "midi_set", channel: intValue(midiEls.channel) }));
midiEls.ccEnabled.addEventListener("change", () => send({ type: "midi_set", cc: { enabled: midiEls.ccEnabled.checked } }));

midiEls.ccNumber.addEventListener("change", () => {
  if (Number.isFinite(intValue(midiEls.ccNumber))) send({ type: "midi_set", cc: { number: intValue(midiEls.ccNumber) } });
  else renderMidi();
});
midiEls.velocityFixed.addEventListener("change", () => send({ type: "midi_set", velocity: { fixed: midiEls.velocityFixed.checked } }));
midiEls.velocityValue.addEventListener("change", () => {
  if (Number.isFinite(intValue(midiEls.velocityValue))) send({ type: "midi_set", velocity: { value: intValue(midiEls.velocityValue) } });
  else renderMidi();
});
midiEls.length.addEventListener("change", () => send({ type: "midi_set", length: midiEls.length.value }));
midiEls.clockEnabled.addEventListener("change", () => send({ type: "midi_set", clock: { enabled: midiEls.clockEnabled.checked } }));
midiEls.clockTransport.addEventListener("change", () => send({ type: "midi_set", clock: { transport: midiEls.clockTransport.checked } }));
midiEls.clockFraction.addEventListener("change", () => send({ type: "midi_set", clock: { fraction: midiEls.clockFraction.value } }));
midiEls.clockSensitivity.addEventListener("input", () => renderClockHint(pulseRate >= 20 && pulseRate <= 300 ? pulseRate : 120));
midiEls.clockSensitivity.addEventListener("change", () => send({ type: "midi_set", clock: { sensitivity: intValue(midiEls.clockSensitivity) } }));
for (const row of midiEls.notes) {
  const kind = row.dataset.kind;
  const [check, number] = row.querySelectorAll("input");
  check.addEventListener("change", () => send({ type: "midi_set", notes: { [kind]: { enabled: check.checked } } }));
  number.addEventListener("change", () => {
    if (Number.isFinite(intValue(number))) send({ type: "midi_set", notes: { [kind]: { note: intValue(number) } } });
    else renderMidi();
  });
}

// --- OSC output ------------------------------------------------------------

const oscEls = {
  host: $("osc-host"),
  port: $("osc-port"),
  connect: $("osc-connect"),
  status: $("osc-status"),
  prefix: $("osc-prefix"),
  rows: [...document.querySelectorAll(".osc-row")],
};
const OSC_NAMES = { wave: "data", systolic: "systolic", notch: "notch", diastolic: "diastolic" };
let osc = null;

function renderOsc() {
  const live = serverUp && osc;
  for (const el of [oscEls.host, oscEls.port, oscEls.connect, oscEls.prefix]) el.disabled = !live;
  for (const row of oscEls.rows) row.querySelector("input").disabled = !live;
  if (!osc) return;

  setValue(oscEls.host, osc.host);
  setValue(oscEls.port, osc.port);
  oscEls.host.disabled = oscEls.port.disabled = !live || osc.connected;  // change them while disconnected
  oscEls.connect.textContent = osc.connected ? "Disconnect" : "Connect";
  oscEls.connect.classList.toggle("active", osc.connected);
  oscEls.status.textContent = osc.error ?? (osc.connected ? `Sending to ${osc.host}:${osc.port}` : "Not connected");
  oscEls.status.classList.toggle("error", Boolean(osc.error));
  setValue(oscEls.prefix, osc.prefix);

  // like the notes: marks the current detector doesn't produce are shown but unavailable
  const kinds = detectors.find((d) => d.id === detection)?.kinds ?? [];
  for (const row of oscEls.rows) {
    const key = row.dataset.osc;
    const check = row.querySelector("input");
    const ok = key === "wave" || kinds.includes(key) || (key === "systolic" && kinds.includes("beat"));
    row.classList.toggle("unavailable", !ok);
    check.disabled = !live || !ok;
    check.checked = key === "wave" ? osc.wave.enabled : osc.events[key].enabled;
    const address = osc.prefix ? `/${osc.prefix}/${OSC_NAMES[key]}` : `/${OSC_NAMES[key]}`;
    const addressEl = row.querySelector(".osc-address");
    addressEl.textContent = addressEl.title = address;
  }
}

oscEls.host.addEventListener("change", () => send({ type: "osc_set", host: oscEls.host.value }));
oscEls.port.addEventListener("change", () => {
  if (Number.isFinite(intValue(oscEls.port))) send({ type: "osc_set", port: intValue(oscEls.port) });
  else renderOsc();
});
oscEls.prefix.addEventListener("change", () => send({ type: "osc_set", prefix: oscEls.prefix.value }));
oscEls.connect.addEventListener("click", () => {
  if (osc.connected) {
    send({ type: "osc_disconnect" });
    saveSettings({ oscConnected: false });
  } else {
    // take whatever is in the fields, even if they haven't fired "change" yet
    send({ type: "osc_set", host: oscEls.host.value, port: intValue(oscEls.port) || osc.port });
    send({ type: "osc_connect" });
    saveSettings({ oscConnected: true });
  }
});
for (const row of oscEls.rows) {
  const key = row.dataset.osc;
  const check = row.querySelector("input");
  check.addEventListener("change", () =>
    send(key === "wave"
      ? { type: "osc_set", wave: { enabled: check.checked } }
      : { type: "osc_set", events: { [key]: { enabled: check.checked } } }));
}

// --- settings kept in the browser ---------------------------------------------
// The server holds the live settings (shared by all pages) but forgets them when
// it restarts. Each page keeps a copy in localStorage and hands it to the server
// when it connects, so they survive restarts of both.

const STORAGE_KEY = "oxibeat.settings";
let restored = false;  // per WebSocket connection

function loadSettings() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) ?? null;
  } catch {
    return null;
  }
}

function saveSettings(patch) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...loadSettings(), ...patch }));
  } catch {
    // storage unavailable (private window, blocked site data): settings just aren't kept
  }
}

function syncSettings(msg) {
  if (restored) {
    const { channel, cc, notes, velocity, length, clock } = msg.midi;
    const { host, port, prefix, wave, events } = msg.osc;
    saveSettings({
      detection: msg.detection,
      midi: { channel, cc, notes, velocity, length, clock },
      osc: { host, port, prefix, wave, events },
    });
    return;
  }
  restored = true;
  const stored = loadSettings();
  if (!stored) {
    syncSettings(msg);  // nothing stored yet: start from the server's settings
    return;
  }
  if (stored.detection && stored.detection !== msg.detection) send({ type: "set_detection", detection: stored.detection });
  if (stored.midi) send({ type: "midi_set", ...stored.midi });
  if (stored.midiPort && msg.midi.port === null && msg.midi.ports.includes(stored.midiPort)) {
    send({ type: "midi_connect", port: stored.midiPort });
  }
  if (stored.osc) send({ type: "osc_set", ...stored.osc });
  if (stored.oscConnected && !msg.osc.connected) send({ type: "osc_connect" });
}

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { serverUp = true; restored = false; render(); };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "wave") onWave(msg.n, msg.samples);
    else if (msg.type === "marks") onMarks(msg.marks);
    else if (msg.type === "vitals") setVitals(msg);
    else if (msg.type === "status") onStatus(msg);
  };
  ws.onclose = () => {
    serverUp = false;
    render();
    setTimeout(connect, 1000);
  };
}

render();
connect();
