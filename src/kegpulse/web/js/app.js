const main = document.querySelector("#main");
const badge = document.querySelector("#connection-badge");
const kegSummary = document.querySelector("#keg-summary");
const banner = document.querySelector("#degraded-banner");
const announcer = document.querySelector("#announcer");
const toast = document.querySelector("#toast");
const menuButton = document.querySelector("#menu-button");
const nav = document.querySelector("#site-nav");
const dialog = document.querySelector("#confirm-dialog");

const state = {
  snapshot: null,
  security: null,
  socket: null,
  pollTimer: null,
  reconnectTimer: null,
  socketFailures: 0,
  lastDevicePhase: null,
  lastSessionId: null,
  completionPour: null,
  completionTimer: null,
  completionPaused: false,
  calibrationDetails: null,
  historyRecords: null,
  historyFilter: "all",
  participantDetails: null,
  serialPorts: null,
  pending: new Set(),
  renderedRoute: null,
};

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

const decimal = (value, fallback = 0) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const formatVolume = (ml, units = state.snapshot?.settings?.display_units || "us_fl_oz") => {
  if (ml === null || ml === undefined || ml === "") return "Unknown volume";
  const value = decimal(ml);
  if (units === "ml") return `${Math.round(value)} mL`;
  if (units === "l") return `${(value / 1000).toFixed(2)} L`;
  return `${(value / 29.5735295625).toFixed(1)} fl oz`;
};

const formatTime = (value) => value ? new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium", timeStyle: "short"
}).format(new Date(value)) : "—";

const uuidKey = () => crypto.randomUUID();

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.remove("hidden");
  toast.style.background = isError ? "#721313" : "#14231d";
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 5000);
}

function showError(error) {
  const message = error instanceof Error ? error.message : String(error);
  showToast(message, true);
  announcer.textContent = `Error: ${message}`;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const method = options.method || "GET";
  if (!["GET", "HEAD"].includes(method)) {
    headers.set("Content-Type", "application/json");
    if (state.security?.csrf_token) headers.set("X-KegPulse-CSRF", state.security.csrf_token);
  }
  const response = await fetch(path, { cache: "no-store", credentials: "same-origin", ...options, method, headers });
  if (!response.ok) {
    let message;
    try {
      const body = await response.json();
      message = Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg).join("; ")
        : body.detail;
    } catch {
      message = await response.text();
    }
    throw new Error(message || `${response.status} ${response.statusText}`);
  }
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("json") ? response.json() : response.text();
}

async function mutation(key, path, body = {}, method = "POST") {
  if (state.pending.has(key)) return null;
  state.pending.add(key);
  render();
  try {
    const result = await api(path, { method, body: JSON.stringify(body) });
    await refresh();
    return result;
  } finally {
    state.pending.delete(key);
    render();
  }
}

function confirmAction(message, label = "Confirm") {
  document.querySelector("#confirm-message").textContent = message;
  document.querySelector("#confirm-accept").textContent = label;
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
  });
}

function route() {
  return location.hash.replace(/^#/, "") || "/";
}

function navigate(path) {
  location.hash = `#${path}`;
}

function connectionText() {
  const connection = state.snapshot?.connection;
  if (!connection) return ["neutral", "● Starting"];
  if (connection.state === "connected") return ["good", "● Device connected"];
  if (connection.state === "degraded") return ["warning", "▲ Resync required"];
  if (["connecting", "reconnecting"].includes(connection.state)) return ["warning", "↻ Reconnecting"];
  return ["bad", "■ Device offline"];
}

function updateChrome() {
  const [kind, label] = connectionText();
  badge.className = `badge ${kind}`;
  badge.textContent = label;
  const inventory = state.snapshot?.inventory;
  const keg = state.snapshot?.keg;
  kegSummary.textContent = keg
    ? `${keg.label}: ${formatVolume(inventory?.remaining_ml)}`
    : "No keg configured";
  const degraded = state.socketFailures > 0 || state.snapshot?.connection?.state !== "connected";
  if (degraded) {
    banner.classList.remove("hidden");
    banner.textContent = state.socketFailures > 0
      ? "Live updates delayed — using status polling. State-changing controls remain guarded by the server."
      : `Flow device ${state.snapshot?.connection?.state || "unavailable"}: ${state.snapshot?.connection?.detail || "waiting"}`;
  } else {
    banner.classList.add("hidden");
  }
  for (const link of nav.querySelectorAll("a")) {
    const target = link.getAttribute("href").slice(1);
    link.toggleAttribute("aria-current", target === route());
  }
}

function reconcileRoute(previous, next) {
  const session = next?.session;
  const deviceState = next?.device?.status?.state;
  if (session && ["arming", "armed", "pouring", "settling", "finalizing"].includes(session.status)) {
    if (route() === "/" || route() === "/complete") navigate("/pour");
  } else if (previous?.session && !session && next?.last_pour?.id !== previous?.last_pour?.id) {
    state.completionPour = next.last_pour;
    navigate("/complete");
  } else if (route() === "/pour" && !session && !next?.pending_capture) {
    navigate("/");
  }
  if (deviceState && deviceState !== state.lastDevicePhase) {
    if (["armed", "pouring", "settling", "complete", "interrupted", "timed_out"].includes(deviceState)) {
      announcer.textContent = `Pour state: ${deviceState.replaceAll("_", " ")}`;
    }
    state.lastDevicePhase = deviceState;
  }
}

function applySnapshot(snapshot) {
  if (state.snapshot && Number(snapshot.revision) < Number(state.snapshot.revision)) return;
  const previous = state.snapshot;
  state.snapshot = snapshot;
  reconcileRoute(previous, snapshot);
  updateChrome();
  const editing = main.contains(document.activeElement)
    && document.activeElement.matches("input, select, textarea");
  if (!editing || route() === "/pour") render();
}

async function refresh() {
  try {
    applySnapshot(await api("/api/v1/status"));
  } catch (error) {
    showError(error);
  }
}

function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = window.setInterval(refresh, 1500);
}

function stopPolling() {
  window.clearInterval(state.pollTimer);
  state.pollTimer = null;
}

function connectSocket() {
  if (state.socket) state.socket.close();
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/api/v1/ws`);
  state.socket = socket;
  socket.addEventListener("open", async () => {
    state.socketFailures = 0;
    stopPolling();
    await refresh();
    updateChrome();
  });
  socket.addEventListener("message", (event) => {
    try { applySnapshot(JSON.parse(event.data)); } catch { startPolling(); }
  });
  socket.addEventListener("close", () => {
    if (state.socket !== socket) return;
    state.socketFailures += 1;
    startPolling();
    updateChrome();
    const delay = Math.min(15000, 500 * (2 ** Math.min(state.socketFailures, 5)));
    state.reconnectTimer = window.setTimeout(connectSocket, delay);
  });
  socket.addEventListener("error", () => socket.close());
}

function page(title, subtitle, content) {
  return `<section><h1 tabindex="-1">${escapeHtml(title)}</h1>${subtitle ? `<p class="lead">${escapeHtml(subtitle)}</p>` : ""}${content}</section>`;
}

function homeView() {
  const s = state.snapshot;
  const onboarding = s.onboarding;
  const inventory = s.inventory;
  const percent = inventory ? Math.max(0, Math.min(100, decimal(inventory.percent_remaining))) : 0;
  const warnings = [];
  if (onboarding.needs_keg) warnings.push(`<li><a href="#/keg">Install the current keg</a> so pours can be assigned to its history.</li>`);
  if (onboarding.needs_calibration) warnings.push(`<li><a href="#/calibration">Complete a ten-pour scale calibration</a>. Pulses captured first are preserved, but their volume and inventory effect remain unknown.</li>`);
  if (inventory?.has_unknown_pours) warnings.push("<li>One or more raw-pulse events have unknown volume and need review.</li>");
  if (decimal(inventory?.remaining_ml) < 0) warnings.push(`<li class="danger-text">Inventory overrun: ${formatVolume(Math.abs(decimal(inventory.remaining_ml)))} beyond the configured keg volume.</li>`);
  const participants = s.participants || [];
  const buttons = participants.map((participant) => `
    <button class="participant-button" data-action="arm" data-participant="${escapeHtml(participant.id)}" ${state.pending.has("arm") ? "disabled" : ""}>
      ${escapeHtml(participant.display_name)}
    </button>`).join("");
  return page("Ready for a pour?", "Select a person before opening the tap, or choose Guest.", `
    ${warnings.length ? `<aside class="card setup-callout" aria-labelledby="setup-title"><h2 id="setup-title">Setup and review</h2><ul>${warnings.join("")}</ul></aside>` : ""}
    <div class="grid two">
      <section class="card" aria-labelledby="keg-title">
        <h2 id="keg-title">${escapeHtml(s.keg?.label || "No current keg")}</h2>
        <div class="metric">${inventory ? formatVolume(inventory.remaining_ml) : "—"}</div>
        <p>${inventory ? `${decimal(inventory.percent_remaining).toFixed(1)}% remaining` : "Configure a keg in Admin."}</p>
        <progress class="progress" max="100" value="${percent}" aria-label="Keg percent remaining">${percent}%</progress>
      </section>
      <section class="card" aria-labelledby="last-title">
        <h2 id="last-title">Last pour</h2>
        ${s.last_pour ? `<div class="metric">${formatVolume(s.last_pour.volume_ml)}</div><p>${escapeHtml(s.last_pour.participant_name || "Guest / Unattributed")} · ${formatTime(s.last_pour.ended_at)}</p><p class="muted">${escapeHtml(s.last_pour.quality.replaceAll("_", " "))} · ${escapeHtml(s.last_pour.raw_pulses)} raw pulses</p>` : '<p class="empty">No pours recorded yet.</p>'}
      </section>
    </div>
    <section class="card" aria-labelledby="people-title">
      <h2 id="people-title">Who is pouring?</h2>
      <div class="participant-grid">
        ${buttons}
        <button class="participant-button guest" data-action="arm" data-participant="" ${state.pending.has("arm") ? "disabled" : ""}>${participants.length === 0 ? "Start pour" : "Guest / Unattributed"}</button>
      </div>
      ${participants.length === 0 ? '<p class="muted">No profiles yet. “Start pour” records an unattributed event; you can assign it later.</p>' : ""}
    </section>
  `);
}

function pourView() {
  const s = state.snapshot;
  const session = s.session || s.pending_capture;
  if (!session) return page("No active pour", "The device has no active session.", '<a class="button" href="#/">Return home</a>');
  const participant = s.participants.find((item) => item.id === session.participant_id);
  const purpose = session.purpose || "pour";
  const status = session.status === "complete"
    ? "complete"
    : (s.device?.status?.state || session.status);
  const volume = s.live_volume_ml;
  const pulses = s.device?.status?.pulses ?? session.captured_raw_pulses ?? 0;
  const afterFlow = decimal(pulses) > 0 || ["pouring", "settling", "finalizing", "complete"].includes(status);
  const title = purpose === "calibration"
    ? `Calibration sample ${session.target_ordinal}`
    : purpose === "verification" ? "Verification pour" : (participant?.display_name || "Guest / Unattributed");
  const cancelLabel = afterFlow ? "End and save partial pour" : "Cancel arming";
  const note = status === "armed" ? "Open the tap before the arming window expires." :
    status === "pouring" ? "Flow detected. Raw pulses are being counted on the device." :
    status === "settling" ? "Flow paused. You may briefly resume before completion." :
    status === "complete" ? "Measurement captured. Enter the scale mass to continue." :
    "Waiting for the authoritative device state.";
  return `<section class="pour-screen"><div class="pour-panel card">
    <p class="pour-state">${escapeHtml(status)}</p>
    <h1 tabindex="-1">${escapeHtml(title)}</h1>
    <div class="pour-amount">${formatVolume(volume)}</div>
    <p class="countdown">${escapeHtml(pulses)} raw pulses</p>
    <p>${escapeHtml(note)}</p>
    ${status === "complete" && purpose !== "pour" ? '<a class="button" href="#/calibration">Enter scale mass</a>' : `
      <button class="${afterFlow ? "danger" : "secondary"}" data-action="cancel" ${state.pending.has("cancel") ? "disabled" : ""}>${cancelLabel}</button>`}
  </div></section>`;
}

function completionView() {
  const pour = state.completionPour || state.snapshot?.last_pour;
  if (!pour) return homeView();
  const warning = pour.quality === "interrupted" || pour.quality === "needs_review";
  scheduleCompletionReturn();
  return page("Pour recorded", "The host has saved this measurement.", `
    <section class="card" data-completion-panel>
      <div class="metric">${formatVolume(pour.volume_ml)}</div>
      <p>${escapeHtml(pour.participant_name || "Guest / Unattributed")} · ${escapeHtml(pour.raw_pulses)} raw pulses</p>
      ${warning ? `<p class="warning-text">Review needed: ${escapeHtml(pour.quality.replaceAll("_", " "))}. Counted pulses were retained.</p>` : '<p class="good-text">Complete measurement saved.</p>'}
      <p id="return-countdown" class="muted">Returning home shortly. Interaction pauses auto-return.</p>
      <div class="button-row"><button data-action="home-now">Return home</button><button class="secondary" data-action="stay">Stay here</button></div>
    </section>`);
}

function scheduleCompletionReturn() {
  window.clearTimeout(state.completionTimer);
  if (state.completionPaused) return;
  const seconds = Number(state.snapshot?.settings?.completion_seconds ?? 9);
  if (seconds > 0) state.completionTimer = window.setTimeout(() => navigate("/"), seconds * 1000);
}

function historyView() {
  return page("Pour history", "Raw pulses, original calibration, keg, timestamps, and quality are retained.", `
    <section class="card stack">
      <div class="button-row">
        <button data-action="load-history">Refresh history</button>
        <a class="button secondary" href="/api/v1/export.csv" download>Export CSV</a>
        <a class="button secondary" href="/api/v1/export.json" download>Export JSON</a>
      </div>
      <label><span>Filter</span><select id="history-filter"><option value="all" ${state.historyFilter === "all" ? "selected" : ""}>All pours</option><option value="unattributed" ${state.historyFilter === "unattributed" ? "selected" : ""}>Unattributed only</option>${state.snapshot.participants.map((p) => `<option value="${escapeHtml(p.id)}" ${state.historyFilter === p.id ? "selected" : ""}>${escapeHtml(p.display_name)}</option>`).join("")}</select></label>
      <div id="history-results" class="${state.historyRecords ? "" : "empty"}">${state.historyRecords ? historyRows(state.historyRecords) : "Choose Refresh history to load recent pours."}</div>
    </section>`);
}

function historyRows(rows) {
  if (!rows.length) return '<p class="empty">No matching pours.</p>';
  return `<div class="table-wrap"><table><thead><tr><th>When</th><th>Person</th><th>Amount</th><th>Evidence</th><th>Action</th></tr></thead><tbody>${rows.map((row) => `
    <tr><td>${formatTime(row.ended_at)}</td><td>${escapeHtml(row.participant_name || "Guest / Unattributed")}</td><td>${formatVolume(row.volume_ml)}</td><td>${escapeHtml(row.raw_pulses)} pulses<br>${escapeHtml(row.quality.replaceAll("_", " "))}</td><td>${row.participant_id ? "Assigned" : `<button class="secondary" data-action="show-reassign" data-pour="${escapeHtml(row.id)}">Assign</button>`}</td></tr>`).join("")}</tbody></table></div>
    <div class="sample-cards">${rows.map((row) => `<article class="sample-card"><strong>${formatVolume(row.volume_ml)}</strong><br>${escapeHtml(row.participant_name || "Guest / Unattributed")}<br><span class="muted">${formatTime(row.ended_at)} · ${escapeHtml(row.raw_pulses)} pulses</span>${row.participant_id ? "" : `<div><button class="secondary" data-action="show-reassign" data-pour="${escapeHtml(row.id)}">Assign</button></div>`}</article>`).join("")}</div>`;
}

function kegView() {
  const keg = state.snapshot.keg;
  const inventory = state.snapshot.inventory;
  return page("Keg inventory", "Replacing a keg closes its history. Manual corrections always require a reason.", `
    <div class="grid two">
      <section class="card"><h2>Current keg</h2>${keg ? `
        <dl class="status-list"><dt>Label</dt><dd>${escapeHtml(keg.label)}</dd><dt>Installed</dt><dd>${formatTime(keg.opened_at)}</dd><dt>Starting</dt><dd>${formatVolume(keg.starting_volume_ml)}</dd><dt>Remaining</dt><dd>${formatVolume(inventory?.remaining_ml)}</dd><dt>Poured</dt><dd>${formatVolume(inventory?.poured_ml)}</dd><dt>Adjustments</dt><dd>${formatVolume(inventory?.adjustments_ml, "ml")}</dd></dl>
        ${decimal(inventory?.remaining_ml) < 0 ? `<p class="danger-text">Overrun: ${formatVolume(Math.abs(decimal(inventory.remaining_ml)))}. Review calibration and adjustments.</p>` : ""}
        ${inventory?.has_unknown_pours ? '<p class="warning-text">Unknown-volume raw pulse evidence exists and is not silently deducted.</p>' : ""}` : '<p class="empty">No keg installed.</p>'}</section>
      <section class="card"><h2>${keg ? "Replace keg" : "Install first keg"}</h2>
        <form id="keg-form" class="stack"><label>Label<input name="label" maxlength="120" required autocomplete="off"></label><label>Starting volume (mL)<input name="starting_volume_ml" type="number" inputmode="decimal" min="1" max="200000" step="0.1" required></label><label>Notes (optional)<textarea name="notes" maxlength="1000"></textarea></label><button>${keg ? "Review and replace" : "Install keg"}</button></form>
      </section>
    </div>
    ${keg ? `<section class="card"><h2>Manual inventory adjustment</h2><form id="adjustment-form" class="grid two"><label>Signed amount (mL)<input name="amount_ml" type="number" inputmode="decimal" step="0.1" min="-200000" max="200000" required><span class="field-help">Positive adds inventory; negative removes it.</span></label><label>Reason<input name="reason" maxlength="500" required></label><button>Review adjustment</button></form></section>` : ""}
  `);
}

function sampleReview(detail) {
  const analysis = detail.analysis;
  const rows = detail.samples.map((sample, index) => {
    const a = analysis?.samples?.[index];
    const flagged = a?.suspected_outlier || sample.suspected_outlier;
    return `<tr class="${flagged ? "outlier" : ""}"><td>${sample.ordinal}</td><td>${sample.raw_pulses}</td><td>${sample.mass_g} g</td><td>${Number(sample.derived_volume_ml).toFixed(2)} mL</td><td>${a ? Number(a.residual_ml).toFixed(2) : "—"} mL</td><td>${flagged ? "Suspected outlier" : "Consistent"}</td><td><button class="secondary" data-action="toggle-sample" data-calibration="${escapeHtml(detail.id)}" data-ordinal="${sample.ordinal}" data-included="${sample.included ? "0" : "1"}">${sample.included ? "Exclude" : "Include"}</button></td></tr>`;
  }).join("");
  const cards = detail.samples.map((sample, index) => {
    const a = analysis?.samples?.[index];
    const flagged = a?.suspected_outlier || sample.suspected_outlier;
    return `<article class="sample-card ${flagged ? "outlier" : ""}"><strong>Sample ${sample.ordinal}</strong><br>${sample.raw_pulses} pulses · ${sample.mass_g} g<br>${flagged ? '<span class="warning-text">Suspected outlier</span>' : "Consistent"}<div><button class="secondary" data-action="toggle-sample" data-calibration="${escapeHtml(detail.id)}" data-ordinal="${sample.ordinal}" data-included="${sample.included ? "0" : "1"}">${sample.included ? "Exclude" : "Include"}</button></div></article>`;
  }).join("");
  return `<section class="card"><h2>Sample review</h2><p>${detail.samples.length}/10 captured · ${analysis?.included_count ?? detail.samples.filter((x) => x.included).length} included. Suspected outliers remain included until you decide.</p>
    <div class="table-wrap"><table><thead><tr><th>#</th><th>Pulses</th><th>Mass</th><th>Scale volume</th><th>Residual</th><th>Consistency</th><th>Include</th></tr></thead><tbody>${rows}</tbody></table></div><div class="sample-cards">${cards}</div>
    ${analysis ? `<p><strong>Aggregate factor:</strong> ${Number(analysis.pulses_per_ml).toFixed(6)} pulses/mL · variation ${Number(analysis.coefficient_of_variation_pct).toFixed(2)}%</p><button data-action="activate-calibration" data-calibration="${escapeHtml(detail.id)}">Review and activate</button>` : '<p class="muted">Capture all ten samples and keep at least seven included to activate.</p>'}
  </section>`;
}

function calibrationView() {
  const active = state.snapshot.active_calibration;
  const capture = state.snapshot.pending_capture;
  const verification = state.snapshot.last_verification;
  return page("Calibration & verification", "Use a tared scale. Mass ÷ density gives volume; KegPulse uses total pulses ÷ total volume.", `
    <section class="card setup-callout"><h2>Ten-pour procedure</h2><ol class="step-list"><li>Tare an empty glass on the scale.</li><li>Use water at 1.000 g/mL first, then repeat with the installed keg and known/approximate beer density.</li><li>Capture ten varied-size pours; enter the scale mass after each.</li><li>Review residuals and explicitly include or exclude suspected outliers.</li><li>Activate only after reviewing the aggregate factor.</li></ol><p class="warning-text">Density directly affects volume. KegPulse is not a legal-for-trade meter.</p></section>
    ${capture?.status === "complete" ? `<section class="card"><h2>${capture.purpose === "verification" ? "Enter verification mass" : `Enter mass for sample ${capture.target_ordinal}`}</h2><p>${escapeHtml(capture.captured_raw_pulses)} raw pulses captured.</p><form id="capture-commit-form" data-purpose="${capture.purpose}" data-session="${capture.session_id}" data-calibration="${capture.calibration_id || ""}" class="grid two"><label>Scale mass (g)<input name="mass_g" type="number" inputmode="decimal" min="0.1" max="10000" step="0.01" required></label><label>Density (g/mL)<input name="density_g_per_ml" type="number" inputmode="decimal" min="0.5" max="2" step="0.001" value="1.000" required></label>${capture.purpose === "calibration" ? '<label><input name="included" type="checkbox" checked> Include this sample</label>' : ""}<button>Save measured check</button></form></section>` : ""}
    ${verification ? `<section class="card ${verification.warning ? "outlier" : ""}"><h2>Latest verification</h2><dl class="status-list"><dt>Predicted</dt><dd>${formatVolume(verification.predicted_volume_ml)}</dd><dt>Scale volume</dt><dd>${formatVolume(verification.actual_volume_ml)}</dd><dt>Absolute error</dt><dd>${formatVolume(verification.absolute_error_ml)}</dd><dt>Percentage error</dt><dd>${Number(verification.percentage_error).toFixed(2)}%</dd></dl><p class="${verification.warning ? "warning-text" : "good-text"}">${verification.warning ? "Drift warning: investigate sensor, flow conditions, tubing, or calibration. The factor was not changed." : "Verification is within the configured warning threshold."}</p></section>` : ""}
    <div class="grid two">
      <section class="card"><h2>Active calibration</h2>${active ? `<dl class="status-list"><dt>Liquid</dt><dd>${escapeHtml(active.liquid)}</dd><dt>Factor</dt><dd>${Number(active.pulses_per_ml).toFixed(6)} pulses/mL</dd><dt>Activated</dt><dd>${formatTime(active.activated_at)}</dd></dl><button data-action="start-verification">Start weighed verification pour</button>` : '<p class="empty">No calibration is active. Complete a run below.</p>'}</section>
      <section class="card"><h2>New calibration run</h2><form id="calibration-form" class="stack"><label>Liquid<input name="liquid" maxlength="80" value="water" required></label><label>Density (g/mL)<input name="density_g_per_ml" type="number" inputmode="decimal" min="0.5" max="2" step="0.001" value="1.000" required></label><label>Notes<textarea name="notes" maxlength="1000"></textarea></label><button>Create ten-pour run</button></form></section>
    </div>
    <section id="calibration-runs" class="stack">${state.calibrationDetails
      ? calibrationRuns(state.calibrationDetails)
      : '<button data-action="load-calibrations" class="secondary">Load calibration runs</button>'}</section>
  `);
}

function calibrationRuns(details) {
  return details.map((detail) => `<article class="card"><h2>${escapeHtml(detail.liquid)} · ${escapeHtml(detail.status)}</h2><p>Created ${formatTime(detail.created_at)}</p>${detail.status === "draft" && detail.samples.length < 10 ? `<button data-action="capture-sample" data-calibration="${escapeHtml(detail.id)}" data-ordinal="${detail.samples.length + 1}">Capture sample ${detail.samples.length + 1}</button>` : ""}${detail.samples.length ? sampleReview(detail) : '<p class="empty">No samples captured yet.</p>'}</article>`).join("");
}

function participantsView() {
  return page("Participants", "Profiles can be renamed or deactivated; historical pours are never deleted.", `
    <div class="grid two"><section class="card"><h2>Add participant</h2><form id="participant-form" class="stack"><label>Display name<input name="display_name" maxlength="80" autocomplete="off" required></label><button>Add participant</button></form></section>
    <section class="card"><h2>Profiles</h2><div id="participant-list" class="stack">${state.participantDetails ? participantList(state.participantDetails) : '<button class="secondary" data-action="load-participants">Load all profiles</button>'}</div></section></div>`);
}

function participantList(items) {
  if (!items.length) return '<p class="empty">No profiles.</p>';
  return items.map((item) => `<form class="participant-edit card" data-id="${escapeHtml(item.id)}"><label>Display name<input name="display_name" maxlength="80" value="${escapeHtml(item.display_name)}" required></label><label><input name="active" type="checkbox" ${item.active ? "checked" : ""}> Active on home screen</label><button class="secondary">Save profile</button></form>`).join("");
}

function settingsView() {
  const s = state.snapshot;
  const device = s.device;
  return page("Device & settings", "Hardware state and recovery information stay visible. LAN mode is configured offline and requires a PIN.", `
    <div class="grid two"><section class="card"><h2>Flow device</h2><dl class="status-list"><dt>Connection</dt><dd>${escapeHtml(s.connection.state)} — ${escapeHtml(s.connection.detail)}</dd><dt>Protocol</dt><dd>${escapeHtml(device.identity.proto || "—")}</dd><dt>Firmware</dt><dd>${escapeHtml(device.identity.fw || "—")}</dd><dt>Device ID</dt><dd>${escapeHtml(device.identity.device || "—")}</dd><dt>Boot ID</dt><dd>${escapeHtml(device.identity.boot || "—")}</dd><dt>State</dt><dd>${escapeHtml(device.status.state || "—")}</dd><dt>Lifetime pulses</dt><dd>${escapeHtml(device.status.lifetime || "0")}</dd><dt>Recovered pulses</dt><dd>${escapeHtml(device.counters?.recovery || "0")}</dd><dt>Device fault</dt><dd>${escapeHtml(device.counters?.fault || "none")}</dd><dt>Rejected noise edges</dt><dd>${escapeHtml(device.counters?.rejected || "0")}</dd><dt>Noise gate</dt><dd>${escapeHtml(device.counters?.noise_gate_us || "0")} µs</dd><dt>Queue overflows</dt><dd>${escapeHtml(s.connection.queue_overflows)}</dd></dl><button data-action="load-ports" class="secondary">Scan serial ports</button><div id="port-results">${state.serialPorts === null ? "" : state.serialPorts.length ? `<ul>${state.serialPorts.map((p) => `<li>${escapeHtml(p.device)} — ${escapeHtml(p.description)}</li>`).join("")}</ul>` : '<p class="empty">No serial ports detected.</p>'}</div></section>
    <section class="card"><h2>Display</h2><form id="settings-form" class="stack"><label>Units<select name="display_units"><option value="us_fl_oz" ${s.settings.display_units === "us_fl_oz" ? "selected" : ""}>US fl oz</option><option value="ml" ${s.settings.display_units === "ml" ? "selected" : ""}>mL</option><option value="l" ${s.settings.display_units === "l" ? "selected" : ""}>Liters</option></select></label><label>Completion display (seconds)<input name="completion_seconds" type="number" min="0" max="60" value="${escapeHtml(s.settings.completion_seconds)}"></label><label>Verification warning (%)<input name="verification_warning_pct" type="number" min="0.1" max="100" step="0.1" value="${escapeHtml(s.settings.verification_warning_pct)}"></label><button>Save settings</button></form></section></div>
    <div class="grid two"><section class="card"><h2>Administrator PIN</h2><p>${state.security?.pin_configured ? "A PIN protects administrative actions." : "No PIN is configured. Anyone with physical access to this loopback kiosk can administer it."}</p>${state.security?.authenticated ? '<p class="good-text">Administrator unlocked for this session.</p>' : '<p class="warning-text">Administrator locked.</p>'}<form id="pin-form" class="stack"><label>${state.security?.pin_configured ? "New PIN" : "PIN"}<input name="pin" type="password" inputmode="numeric" minlength="6" maxlength="20" pattern="[0-9]+" autocomplete="new-password" required></label><button>${state.security?.pin_configured ? "Change PIN" : "Set PIN"}</button></form>${state.security?.pin_configured && !state.security?.authenticated ? '<form id="login-form" class="stack"><label>Unlock with PIN<input name="pin" type="password" inputmode="numeric" minlength="6" maxlength="20" pattern="[0-9]+" autocomplete="current-password" required></label><button>Unlock administrator</button></form>' : ""}</section>
    <section class="card"><h2>Data & privacy</h2><p>Database, logs, backups, and exports remain on this device. Backups are not encrypted; store them securely.</p><button data-action="backup">Create atomic backup</button><a class="button secondary" href="/api-docs">Local API schema</a><p>Network mode: <strong>${s.settings?.lan_mode ? "trusted LAN" : "loopback only"}</strong>. No telemetry or cloud dependency.</p></section></div>
    ${s.mode === "demo" ? demoPanel() : ""}
  `);
}

function demoPanel() {
  return `<section class="card" aria-labelledby="demo-title"><h2 id="demo-title">Demo simulator controls</h2><p class="warning-text">Demo mode is explicit. These controls do not exist in hardware mode.</p><div class="button-row"><button data-action="demo-pulse" data-count="25">Add 25 pulses</button><button data-action="demo-finish">Finish pour</button><button class="secondary" data-action="demo-disconnect">Disconnect</button><button class="secondary" data-action="demo-reconnect">Reconnect</button><button class="danger" data-action="demo-reset">Reset device</button></div><fieldset><legend>Next-frame fault</legend><div class="button-row"><button class="secondary" data-action="demo-fault" data-fault="corrupt_next">Corrupt</button><button class="secondary" data-action="demo-fault" data-fault="duplicate_next">Duplicate</button><button class="secondary" data-action="demo-fault" data-fault="delay_next">Delay</button><button class="secondary" data-action="demo-flush">Flush delayed</button></div></fieldset></section>`;
}

function loginView() {
  return page("Administrator login", "This device requires a PIN before local data and controls are shown.", `<section class="card"><form id="login-form" class="stack"><label>Admin PIN<input name="pin" type="password" inputmode="numeric" minlength="6" maxlength="20" pattern="[0-9]+" autocomplete="current-password" required></label><button>Unlock KegPulse</button></form></section>`);
}

function render() {
  if (!state.snapshot) {
    if (state.security?.lan_mode && !state.security?.authenticated) {
      main.innerHTML = loginView();
      bindForms();
      main.querySelector("h1")?.focus({ preventScroll: true });
    }
    return;
  }
  const shouldFocus = state.renderedRoute !== route();
  if (state.snapshot.settings?.lan_mode && !state.security?.authenticated) {
    main.innerHTML = loginView();
  } else {
    const current = route();
    if (current === "/") main.innerHTML = homeView();
    else if (current === "/pour") main.innerHTML = pourView();
    else if (current === "/complete") main.innerHTML = completionView();
    else if (current === "/history") main.innerHTML = historyView();
    else if (current === "/keg") main.innerHTML = kegView();
    else if (current === "/calibration") main.innerHTML = calibrationView();
    else if (current === "/participants") main.innerHTML = participantsView();
    else if (current === "/settings") main.innerHTML = settingsView();
    else main.innerHTML = page("Not found", "That screen does not exist.", '<a class="button" href="#/">Return home</a>');
  }
  bindForms();
  if (shouldFocus) main.querySelector("h1")?.focus({ preventScroll: true });
  state.renderedRoute = route();
}

async function arm(participantId) {
  try {
    await mutation("arm", "/api/v1/sessions/arm", { participant_id: participantId || null, idempotency_key: uuidKey() });
    navigate("/pour");
  } catch (error) { showError(error); }
}

async function cancelPour() {
  const pulses = decimal(state.snapshot?.device?.status?.pulses);
  if (pulses > 0) {
    const accepted = await confirmAction("Counted pulses will be retained and saved as an interrupted partial pour. End now?", "End and save partial");
    if (!accepted) return;
  }
  try { await mutation("cancel", "/api/v1/sessions/cancel"); } catch (error) { showError(error); }
}

async function loadHistory() {
  const filter = document.querySelector("#history-filter")?.value || "all";
  const query = filter === "unattributed" ? "?unattributed_only=true" : filter === "all" ? "" : `?participant_id=${encodeURIComponent(filter)}`;
  try {
    const rows = await api(`/api/v1/history${query}`);
    state.historyFilter = filter;
    state.historyRecords = rows;
    render();
  } catch (error) { showError(error); }
}

async function showReassign(pourId) {
  const names = state.snapshot.participants.map((p) => `${p.display_name} (${p.id})`).join("\n");
  const chosen = window.prompt(`Enter participant ID:\n${names}`);
  if (!chosen) return;
  const reason = window.prompt("Reason for reassignment:", "Confirmed by administrator");
  if (!reason) return;
  try { await mutation(`assign-${pourId}`, `/api/v1/history/${pourId}/reassign`, { participant_id: chosen.trim(), reason }); await loadHistory(); } catch (error) { showError(error); }
}

async function loadCalibrations() {
  try {
    const runs = await api("/api/v1/calibrations");
    const details = await Promise.all(runs.map((run) => api(`/api/v1/calibrations/${run.id}`)));
    state.calibrationDetails = details;
    render();
  } catch (error) { showError(error); }
}

async function captureSample(calibrationId, ordinal) {
  try {
    await mutation("capture", `/api/v1/calibrations/${calibrationId}/capture/arm`, { idempotency_key: uuidKey(), ordinal: Number(ordinal) });
    navigate("/pour");
  } catch (error) { showError(error); }
}

async function startVerification() {
  try {
    await mutation("verify", "/api/v1/verifications/capture/arm", { idempotency_key: uuidKey() });
    navigate("/pour");
  } catch (error) { showError(error); }
}

async function demo(action, values = {}) {
  try { await mutation(`demo-${action}`, "/api/v1/demo/action", { action, ...values }); } catch (error) { showError(error); }
}

function bindForms() {
  document.querySelector("#participant-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try { await mutation("participant", "/api/v1/participants", { display_name: form.get("display_name") }); showToast("Participant added"); render(); } catch (error) { showError(error); }
  });
  document.querySelector("#keg-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    const accepted = await confirmAction("The current keg will be closed and preserved in history. Install this new keg?", "Install keg");
    if (!accepted) return;
    try { await mutation("keg", "/api/v1/kegs/replace", { label: form.get("label"), starting_volume_ml: form.get("starting_volume_ml"), notes: form.get("notes") }); showToast("Keg installed"); } catch (error) { showError(error); }
  });
  document.querySelector("#adjustment-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    const accepted = await confirmAction(`Apply a ${form.get("amount_ml")} mL inventory adjustment? The reason is permanently audited.`, "Apply adjustment");
    if (!accepted) return;
    try { await mutation("adjust", `/api/v1/kegs/${state.snapshot.keg.id}/adjustments`, { amount_ml: form.get("amount_ml"), reason: form.get("reason") }); showToast("Adjustment recorded"); } catch (error) { showError(error); }
  });
  document.querySelector("#calibration-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try { await mutation("calibration", "/api/v1/calibrations", { liquid: form.get("liquid"), density_g_per_ml: form.get("density_g_per_ml"), notes: form.get("notes") }); showToast("Calibration run created"); await loadCalibrations(); } catch (error) { showError(error); }
  });
  document.querySelector("#capture-commit-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const element = event.currentTarget; const form = new FormData(element);
    const body = { session_id: element.dataset.session, mass_g: form.get("mass_g"), density_g_per_ml: form.get("density_g_per_ml"), included: form.get("included") !== null };
    const url = element.dataset.purpose === "verification" ? "/api/v1/verifications/capture/commit" : `/api/v1/calibrations/${element.dataset.calibration}/capture/commit`;
    try { const result = await mutation("capture-commit", url, body); showToast(result?.warning ? `Verification saved: ${Number(result.percentage_error).toFixed(2)}% error — drift warning` : "Scale measurement saved"); await loadCalibrations(); } catch (error) { showError(error); }
  });
  document.querySelector("#settings-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try { await mutation("settings", "/api/v1/settings", { display_units: form.get("display_units"), completion_seconds: Number(form.get("completion_seconds")), verification_warning_pct: form.get("verification_warning_pct") }, "PATCH"); showToast("Settings saved"); } catch (error) { showError(error); }
  });
  document.querySelector("#pin-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try { await mutation("pin", "/api/v1/security/pin", { pin: form.get("pin") }, "PUT"); state.security = await api("/api/v1/security/context"); showToast("Administrator PIN updated; unlock again with the new PIN"); render(); } catch (error) { showError(error); }
  });
  document.querySelector("#login-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try { state.security = await api("/api/v1/security/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin: form.get("pin") }) }); await refresh(); if (!state.socket) connectSocket(); showToast("Administrator unlocked"); } catch (error) { showError(error); }
  });
  for (const form of document.querySelectorAll(".participant-edit")) form.addEventListener("submit", async (event) => {
    event.preventDefault(); const element = event.currentTarget; const data = new FormData(element);
    try { await mutation(`profile-${element.dataset.id}`, `/api/v1/participants/${element.dataset.id}`, { display_name: data.get("display_name"), active: data.get("active") !== null }, "PATCH"); await loadParticipants(); } catch (error) { showError(error); }
  });
}

async function loadParticipants() {
  try { state.participantDetails = await api("/api/v1/participants?include_inactive=true"); render(); } catch (error) { showError(error); }
}

main.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]"); if (!button) return;
  const action = button.dataset.action;
  if (action === "retry") return window.location.reload();
  if (action === "arm") return arm(button.dataset.participant);
  if (action === "cancel") return cancelPour();
  if (action === "home-now") { state.completionPaused = false; return navigate("/"); }
  if (action === "stay") { state.completionPaused = true; window.clearTimeout(state.completionTimer); document.querySelector("#return-countdown")?.classList.add("hidden"); return; }
  if (action === "load-history") return loadHistory();
  if (action === "show-reassign") return showReassign(button.dataset.pour);
  if (action === "load-calibrations") return loadCalibrations();
  if (action === "capture-sample") return captureSample(button.dataset.calibration, button.dataset.ordinal);
  if (action === "toggle-sample") { try { await mutation(`sample-${button.dataset.ordinal}`, `/api/v1/calibrations/${button.dataset.calibration}/samples/${button.dataset.ordinal}`, { included: button.dataset.included === "1" }, "PATCH"); await loadCalibrations(); } catch (error) { showError(error); } return; }
  if (action === "activate-calibration") { const accepted = await confirmAction("Activate this reviewed factor? Historical pours will keep their original calibration.", "Activate calibration"); if (accepted) { try { await mutation("activate", `/api/v1/calibrations/${button.dataset.calibration}/activate`); showToast("Calibration activated"); await loadCalibrations(); } catch (error) { showError(error); } } return; }
  if (action === "start-verification") return startVerification();
  if (action === "load-participants") return loadParticipants();
  if (action === "load-ports") { try { state.serialPorts = await api("/api/v1/serial/ports"); render(); } catch (error) { showError(error); } return; }
  if (action === "backup") { try { const result = await mutation("backup", "/api/v1/backup"); showToast(`Backup created: ${result.filename}`); } catch (error) { showError(error); } return; }
  if (action === "demo-pulse") return demo("pulse", { count: Number(button.dataset.count) });
  if (action === "demo-finish") return demo("finish");
  if (action === "demo-disconnect") return demo("disconnect");
  if (action === "demo-reconnect") return demo("reconnect");
  if (action === "demo-reset") { const accepted = await confirmAction("Reset the simulated device boot identity? Active flow becomes uncertain.", "Reset simulator"); if (accepted) return demo("reset"); return; }
  if (action === "demo-fault") return demo("fault", { fault: button.dataset.fault, enabled: true });
  if (action === "demo-flush") return demo("flush");
});

main.addEventListener("focusin", (event) => {
  if (route() === "/complete" && event.target.matches("button, a, input, select, textarea")) {
    state.completionPaused = true;
    window.clearTimeout(state.completionTimer);
  }
});
main.addEventListener("change", (event) => {
  if (event.target.matches("#history-filter")) loadHistory();
});
document.addEventListener("pointerdown", () => { if (route() === "/complete") state.completionPaused = true; }, { passive: true });

menuButton.addEventListener("click", () => {
  const open = !nav.classList.contains("open");
  nav.classList.toggle("open", open);
  menuButton.setAttribute("aria-expanded", String(open));
});
nav.addEventListener("click", () => { nav.classList.remove("open"); menuButton.setAttribute("aria-expanded", "false"); });
window.addEventListener("hashchange", () => { state.completionPaused = false; render(); updateChrome(); });

async function initialize() {
  try {
    state.security = await api("/api/v1/security/context");
    if (state.security.lan_mode && !state.security.authenticated) {
      render();
    } else {
      await refresh();
      connectSocket();
    }
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  } catch (error) {
    main.innerHTML = page("KegPulse service unavailable", "The browser cannot confirm hardware or data while the local service is down.", `<section class="card"><p>${escapeHtml(error.message)}</p><button data-action="retry">Try again</button></section>`);
    startPolling();
  }
}

initialize();
