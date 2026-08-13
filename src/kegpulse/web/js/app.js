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
  securityRequest: null,
  hostAvailable: null,
  hostError: null,
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
  diagnostics: null,
  reassignPourId: null,
  lastAnnouncedPulses: null,
  lastMeasurementAnnouncement: 0,
  dismissedTerminalId: null,
  dialogInvoker: null,
  pending: new Set(),
  dismissedDemoGuides: new Set(),
  renderedRoute: null,
};

const DEMO_GUIDE_STORAGE_PREFIX = "kegpulse.demo-guide.dismissed.";
const DEMO_GUIDES = {
  "/": {
    eyebrow: "Demo tour · 1 of 6",
    title: "Start from the dashboard",
    purpose: "Use this page to confirm the simulator is ready, check inventory, and choose who owns the next pour.",
    steps: [
      "Check that Flow device says Idle; setup warnings explain anything still missing.",
      "Select a participant, or Guest / Unattributed, before simulating flow.",
      "When the live screen opens, visit Device & Settings to add pulses and finish the pour.",
    ],
    previous: ["/history", "Pour history"],
    next: ["/keg", "Set up a keg"],
  },
  "/keg": {
    eyebrow: "Demo tour · 2 of 6",
    title: "Give the demo an inventory",
    purpose: "Install a pretend keg so measured pours can be attached to a keg and deducted from its starting volume.",
    steps: [
      "Enter a recognizable label, starting volume in mL, and installation time.",
      "Submit the form and confirm the current and remaining amounts appear on the left.",
      "Use a signed adjustment only to model a known correction, and always record its reason.",
    ],
    previous: ["/", "Dashboard"],
    next: ["/calibration", "Calibrate"],
  },
  "/calibration": {
    eyebrow: "Demo tour · 3 of 6",
    title: "Teach KegPulse the pulse-to-volume factor",
    purpose: "A ten-pour scale run turns preserved raw pulses into volume without guessing or rewriting earlier evidence.",
    steps: [
      "Create a water run at 1.000 g/mL, then start its first sample.",
      "On Device & Settings, add varied pulse batches and finish; back here, enter mass using the simulator's 5 pulses/mL factor (250 pulses = 50.00 g of water).",
      "Repeat for ten samples, review every residual and inclusion choice, then activate with at least seven included samples.",
      "After activation, try a weighed verification; it checks drift without changing the factor.",
    ],
    previous: ["/keg", "Keg inventory"],
    next: ["/participants", "Add people"],
  },
  "/participants": {
    eyebrow: "Demo tour · 4 of 6",
    title: "Add people without changing history",
    purpose: "Profiles make new pours easy to attribute while old records remain intact when a profile changes.",
    steps: [
      "Add a display name for someone who will appear on the dashboard.",
      "Load all profiles to rename one or toggle whether it remains active on the home screen.",
      "Return to the dashboard and select the new profile before starting a demo pour.",
    ],
    previous: ["/calibration", "Calibration"],
    next: ["/settings", "Run the simulator"],
  },
  "/settings": {
    eyebrow: "Demo tour · 5 of 6",
    title: "Drive the virtual flow meter",
    purpose: "The demo controls generate the same checked protocol events the host expects from the Nano, without touching hardware.",
    steps: [
      "Inspect the simulated device identity, boot ID, state, pulse counters, and timing source.",
      "During an armed pour or calibration sample, use Add 25 pulses as often as needed, then choose Finish pour.",
      "Try disconnect/reconnect or a next-frame fault to see recovery behavior; Reset device deliberately changes its boot identity.",
      "Save display units or timing preferences, then inspect the resulting record in Pour history.",
    ],
    previous: ["/participants", "Participants"],
    next: ["/history", "Review history"],
  },
  "/history": {
    eyebrow: "Demo tour · 6 of 6",
    title: "Audit what the demo recorded",
    purpose: "This page shows the durable pour ledger, including its raw evidence and the context used for each measurement.",
    steps: [
      "Refresh history, then filter by participant or show only unattributed pours.",
      "Open Measurement details to inspect pulses, keg, calibration, device, boot, event, and fault evidence.",
      "Assign a guest pour with a reason, or export CSV/JSON to inspect the same records outside the kiosk.",
    ],
    previous: ["/settings", "Device & Settings"],
    next: ["/", "Finish at dashboard"],
  },
  "/complete": {
    eyebrow: "Completed-pour guide",
    title: "Confirm what was saved",
    purpose: "The completion screen summarizes the durable measurement before the kiosk returns home.",
    steps: [
      "Check the volume, participant attribution, raw pulse count, and quality message.",
      "Choose Stay here, or interact with the page, to pause the automatic return timer.",
      "Open Pour history next to inspect the full device, keg, and calibration evidence.",
    ],
    previous: ["/", "Dashboard"],
    next: ["/history", "Pour history"],
  },
};

function currentDemoSession() {
  return state.snapshot?.session
    || state.snapshot?.pending_capture
    || state.snapshot?.terminal_notice
    || null;
}

function liveDemoGuide() {
  const session = currentDemoSession();
  if (!session) {
    return {
      eyebrow: "Live-flow guide",
      title: "No demo measurement is active",
      purpose: "The simulator has no active pour or scale capture to show on this screen.",
      steps: [
        "Return to the dashboard to arm a person or Guest pour.",
        "For a scale capture, start a sample or verification from Calibration instead.",
        "Wait for the visible Armed state before adding simulator pulses.",
      ],
      previous: ["/history", "Pour history"],
      next: ["/", "Start at dashboard"],
    };
  }
  const purpose = session.purpose || "pour";
  const terminal = ["complete", "timed_out", "interrupted_uncertain"].includes(session.status);
  const status = terminal
    ? session.status
    : (state.snapshot?.device?.status?.state || session.status);
  if (status === "timed_out") {
    if (["calibration", "verification"].includes(purpose)) {
      const label = purpose === "calibration" ? "Calibration sample" : "Verification";
      return {
        eyebrow: `${label} timeout guide`,
        title: `${label} timed out`,
        purpose: "No simulator pulse arrived before the arming deadline, so no scale measurement was created.",
        steps: [
          "Return to Calibration; there is no mass to enter for this attempt.",
          "Start the sample again, then open Simulator controls before the countdown expires.",
          "Add pulses before choosing Finish pour, then enter the tared scale mass.",
        ],
        previous: ["/settings", "Simulator controls"],
        next: ["/calibration", "Return to Calibration"],
      };
    }
    return {
      eyebrow: "Arming-timeout guide",
      title: "No simulated flow arrived",
      purpose: "The arming window closed with zero pulses, so KegPulse did not create a pour or scale sample.",
      steps: [
        "Choose Return home; there is no measurement to recover or assign.",
        "Arm again and open Device & Settings before the countdown expires.",
        "Add pulses first, then choose Finish pour after the intended amount.",
      ],
      previous: ["/settings", "Simulator controls"],
      next: ["/", "Try again"],
    };
  }
  if (status === "interrupted_uncertain") {
    if (["calibration", "verification"].includes(purpose)) {
      const label = purpose === "calibration" ? "calibration sample" : "verification";
      return {
        eyebrow: "Scale-capture recovery guide",
        title: `Review the interrupted ${label}`,
        purpose: "The simulator became unavailable before this scale capture could finish, so KegPulse will not invent a sample.",
        steps: [
          "Reconnect the simulator and wait for its current boot identity and state.",
          "Return to Calibration and confirm that no measured check was silently created.",
          "Retry the capture from Calibration when the simulator is stable.",
        ],
        previous: ["/settings", "Reconnect simulator"],
        next: ["/calibration", "Return to Calibration"],
      };
    }
    return {
      eyebrow: "Recovery guide",
      title: "Review an interrupted measurement",
      purpose: "The host cannot safely invent what happened after the simulated device became unavailable.",
      steps: [
        "Reconnect the simulator from Device & Settings and wait for a fresh identity and status.",
        "Keep the interrupted evidence; do not replace it with a guessed volume.",
        "Review Pour history and recent diagnostics after reconnecting.",
      ],
      previous: ["/settings", "Reconnect simulator"],
      next: ["/history", "Review evidence"],
    };
  }
  if (purpose === "calibration") {
    return status === "complete"
      ? {
        eyebrow: "Calibration-capture guide",
        title: "Enter the weighed sample",
        purpose: "The simulator captured raw pulses, but calibration needs the scale mass before this sample is durable.",
        steps: [
          "Choose Enter scale mass to return to Calibration.",
          "Enter the tared mass and confirm the displayed liquid density.",
          "Save the sample, then repeat until all ten varied pours are captured.",
        ],
        previous: ["/settings", "Simulator controls"],
        next: ["/calibration", "Open Calibration page"],
      }
      : {
        eyebrow: "Calibration-capture guide",
        title: "Simulate a weighed calibration pour",
        purpose: "This capture stores raw pulses for one sample; it does not create a participant pour or reduce inventory.",
        steps: [
          "Watch for Armed, then open Device & Settings before the countdown expires.",
          "Add a varied pulse batch and choose Finish pour when the sample is complete.",
          "Return to Calibration and enter the tared scale mass for this sample.",
        ],
        previous: ["/calibration", "Calibration"],
        next: ["/settings", "Simulator controls"],
      };
  }
  if (purpose === "verification") {
    return status === "complete"
      ? {
        eyebrow: "Verification guide",
        title: "Enter the verification mass",
        purpose: "The pulse capture is complete; the scale mass will compare actual and predicted volume without changing the factor.",
        steps: [
          "Choose Enter scale mass and confirm the density used for the liquid.",
          "Save the weighed check and review its absolute and percentage error.",
          "Investigate a warning rather than silently changing the active calibration.",
        ],
        previous: ["/settings", "Simulator controls"],
        next: ["/calibration", "Open Calibration page"],
      }
      : {
        eyebrow: "Verification guide",
        title: "Simulate a weighed verification pour",
        purpose: "This capture checks calibration drift; it will not create a participant pour or change inventory.",
        steps: [
          "Watch for Armed, then add a known pulse batch from Device & Settings.",
          "Choose Finish pour after the intended test volume.",
          "Return to Calibration and enter the tared scale mass to compare prediction and reality.",
        ],
        previous: ["/calibration", "Calibration"],
        next: ["/settings", "Simulator controls"],
      };
  }
  return {
    eyebrow: "Live-flow guide",
    title: "Watch an authoritative measurement",
    purpose: "This screen follows the simulator's current state and pulse count for the selected person or Guest.",
    steps: [
      "Confirm the attribution and watch for Armed before the countdown expires.",
      "Open Device & Settings, add one or more pulse batches, then choose Finish pour.",
      "Cancelling after pulses arrive saves a reviewable partial measurement instead of discarding it.",
    ],
    previous: ["/", "Dashboard"],
    next: ["/settings", "Simulator controls"],
  };
}

function settingsDemoGuide() {
  const session = currentDemoSession();
  if (!session) return DEMO_GUIDES["/settings"];
  const guide = liveDemoGuide();
  const terminal = ["complete", "timed_out", "interrupted_uncertain"].includes(session.status);
  if (terminal) return guide;
  return {
    ...guide,
    next: ["/settings", "Use simulator controls"],
    nextAction: "demo-guide-controls",
  };
}

function demoGuideContext(path) {
  if (path === "/pour") return { key: path, guide: liveDemoGuide() };
  if (path === "/settings") return { key: path, guide: settingsDemoGuide() };
  if (path === "/complete" && !(state.completionPour || state.snapshot?.last_pour)) {
    return { key: "/", guide: DEMO_GUIDES["/"] };
  }
  return { key: path, guide: DEMO_GUIDES[path] };
}

dialog.addEventListener("cancel", () => { dialog.returnValue = "cancel"; });
dialog.addEventListener("close", () => {
  if (state.dialogInvoker?.isConnected) state.dialogInvoker.focus({ preventScroll: true });
  state.dialogInvoker = null;
});

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

const localDateTimeInput = (value = new Date()) => {
  const date = value instanceof Date ? value : new Date(value);
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 23);
};

const uuidKey = () => crypto.randomUUID();

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

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
    const contentType = response.headers.get("content-type") || "";
    let message = "";
    if (contentType.includes("json")) {
      try {
        const body = await response.json();
        message = Array.isArray(body.detail)
          ? body.detail.map((item) => item.msg).join("; ")
          : body.detail;
      } catch {
        message = "The service returned an unreadable error response.";
      }
    } else {
      message = await response.text();
    }
    throw new ApiError(message || `${response.status} ${response.statusText}`, response.status);
  }
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("json") ? response.json() : response.text();
}

async function mutation(key, path, body = {}, method = "POST") {
  if (state.hostAvailable === false) {
    throw new Error("The KegPulse host is unavailable. Wait for status recovery before trying again.");
  }
  if (state.pending.has(key)) return null;
  state.pending.add(key);
  const unlock = lockActiveMutationSurface();
  let securityRefreshed = false;
  try {
    let result;
    try {
      result = await api(path, { method, body: JSON.stringify(body) });
    } catch (error) {
      const previousToken = state.security?.csrf_token;
      if (!(error instanceof ApiError) || ![401, 403].includes(error.status)) throw error;
      const context = await refreshSecurityContext();
      securityRefreshed = true;
      if (context.lan_mode && !context.authenticated) {
        enterLoginMode();
        throw new ApiError("Administrator login required", 401);
      }
      if (!context.csrf_token || context.csrf_token === previousToken) throw error;
      result = await api(path, { method, body: JSON.stringify(body) });
    }
    await refresh();
    state.pending.delete(key);
    unlock();
    render();
    return result;
  } catch (error) {
    state.pending.delete(key);
    unlock();
    if (securityRefreshed) syncSecurityUi();
    syncHostControls();
    throw error;
  }
}

function lockActiveMutationSurface() {
  const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const form = active?.closest("form");
  const controls = form
    ? [...form.querySelectorAll('button:not([type="button"]), button[type="submit"]')]
    : active instanceof HTMLButtonElement ? [active] : [];
  const disabled = controls.map((control) => control.disabled);
  form?.setAttribute("aria-busy", "true");
  controls.forEach((control) => { control.disabled = true; });
  return () => {
    form?.removeAttribute("aria-busy");
    controls.forEach((control, index) => {
      if (control.isConnected) control.disabled = disabled[index];
    });
  };
}

async function refreshSecurityContext() {
  if (state.securityRequest) return state.securityRequest;
  state.securityRequest = api("/api/v1/security/context")
    .then((context) => {
      state.security = context;
      return context;
    })
    .finally(() => { state.securityRequest = null; });
  return state.securityRequest;
}

function enterLoginMode() {
  window.clearTimeout(state.reconnectTimer);
  stopPolling();
  const socket = state.socket;
  state.socket = null;
  if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
  state.hostAvailable = true;
  state.hostError = null;
  updateChrome();
  render();
}

function loginFormMarkup(embedded = false) {
  const label = embedded ? "Unlock with PIN" : "Admin PIN";
  const button = embedded ? "Unlock administrator" : "Unlock KegPulse";
  return `<form id="login-form" class="stack"><label>${label}<input name="pin" type="password" inputmode="numeric" minlength="6" maxlength="20" pattern="[0-9]+" autocomplete="current-password" required></label><button>${button}</button></form>`;
}

function syncSecurityUi() {
  updateChrome();
  const status = document.querySelector("#admin-auth-status");
  const loginSlot = document.querySelector("#admin-login-slot");
  if (status && loginSlot) {
    const authenticated = Boolean(state.security?.authenticated);
    status.className = authenticated ? "good-text" : "warning-text";
    status.textContent = authenticated
      ? "Administrator unlocked for this session."
      : "Administrator locked.";
    loginSlot.innerHTML = state.security?.pin_configured && !authenticated
      ? loginFormMarkup(true)
      : "";
    bindLoginForm();
    syncHostControls();
  } else if (state.renderedRoute === "__login__" && state.security?.authenticated) {
    render();
  }
}

function confirmAction(message, label = "Confirm") {
  state.dialogInvoker = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  document.querySelector("#confirm-message").textContent = message;
  document.querySelector("#confirm-accept").textContent = label;
  dialog.returnValue = "cancel";
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
  if (state.hostAvailable === false) return ["bad", "■ Host unavailable"];
  if (state.security?.lan_mode && !state.security?.authenticated) {
    return ["warning", "▲ Admin login required"];
  }
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
  const loginRequired = state.security?.lan_mode && !state.security?.authenticated;
  const degraded = state.socketFailures > 0 || state.snapshot?.connection?.state !== "connected";
  if (state.hostAvailable === false) {
    banner.className = "banner danger";
    banner.textContent = "KegPulse host unavailable — displayed information may be stale. Pour and administrative controls are disabled until a fresh status snapshot arrives.";
  } else if (loginRequired) {
    banner.className = "banner warning";
    banner.textContent = "Administrator login required before live status and controls can resume.";
  } else if (degraded) {
    banner.className = "banner warning";
    banner.textContent = state.socketFailures > 0
      ? "Live updates delayed — using status polling. State-changing controls remain guarded by the server."
      : `Flow device ${state.snapshot?.connection?.state || "unavailable"}: ${state.snapshot?.connection?.detail || "waiting"}`;
  } else {
    banner.className = "banner warning hidden";
  }
  for (const link of nav.querySelectorAll("a")) {
    const target = link.getAttribute("href").slice(1);
    if (target === route()) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  syncHostControls();
}

function syncHostControls() {
  const controls = [...main.querySelectorAll("button"), document.querySelector("#confirm-accept")]
    .filter((control) => control
      && control.dataset.action !== "retry"
      && control.dataset.hostIndependent !== "true");
  for (const control of controls) {
    if (state.hostAvailable === false && !control.disabled) {
      control.dataset.hostDisabled = "true";
      control.disabled = true;
    } else if (state.hostAvailable !== false && control.dataset.hostDisabled === "true") {
      control.disabled = false;
      delete control.dataset.hostDisabled;
    }
  }
}

function setHostAvailability(available, error = null) {
  const previous = state.hostAvailable;
  const changed = previous !== available;
  state.hostAvailable = available;
  state.hostError = available ? null : error;
  updateChrome();
  if (!state.snapshot && changed) render();
  if (changed && (available === false || previous === false)) {
    announcer.textContent = available
      ? "KegPulse host connection restored. Fresh status received."
      : "KegPulse host unavailable. Displayed information may be stale and controls are disabled.";
  }
}

function reconcileRoute(previous, next) {
  const session = next?.session;
  const terminal = next?.terminal_notice;
  const deviceState = next?.device?.status?.state;
  if (session && ["arming", "armed", "pouring", "settling", "finalizing"].includes(session.status)) {
    state.dismissedTerminalId = null;
    if (route() === "/" || route() === "/complete") navigate("/pour");
  } else if (
    ["timed_out", "interrupted_uncertain"].includes(terminal?.status)
    && terminal.session_id !== state.dismissedTerminalId
    && ["/", "/pour"].includes(route())
  ) {
    if (route() !== "/pour") navigate("/pour");
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
  const pulses = next?.device?.status?.pulses;
  if (
    deviceState === "pouring"
    && pulses !== undefined
    && pulses !== state.lastAnnouncedPulses
    && performance.now() - state.lastMeasurementAnnouncement >= 5000
  ) {
    announcer.textContent = `Measured ${formatVolume(next.live_volume_ml)} from ${pulses} pulses.`;
    state.lastAnnouncedPulses = pulses;
    state.lastMeasurementAnnouncement = performance.now();
  }
}

function applySnapshot(snapshot) {
  if (state.snapshot && Number(snapshot.revision) < Number(state.snapshot.revision)) return;
  const previousGuideTitle = route() === "/settings" && state.snapshot?.mode === "demo"
    ? demoGuideContext("/settings").guide?.title
    : null;
  const previous = state.snapshot;
  state.snapshot = snapshot;
  reconcileRoute(previous, snapshot);
  updateChrome();
  // A snapshot may arrive between a user's final field edit and the form's
  // submit click. Keep the whole focused form intact so its values and click
  // target cannot be replaced mid-interaction.
  const editing = (
    main.contains(document.activeElement)
    && document.activeElement.closest("form") !== null
  ) || (dialog.open && state.dialogInvoker && main.contains(state.dialogInvoker));
  const rendered = !editing || route() === "/pour";
  if (rendered) render();
  if (rendered && route() === "/settings" && snapshot.mode === "demo") {
    const session = currentDemoSession();
    const guide = demoGuideContext("/settings").guide;
    const scaleHandoff = (
      ["calibration", "verification"].includes(session?.purpose)
      && ["complete", "timed_out", "interrupted_uncertain"].includes(session?.status)
      && guide?.title !== previousGuideTitle
    );
    if (scaleHandoff) {
      main.querySelector("#demo-guide")?.focus({ preventScroll: true });
      announcer.textContent = `${guide.title}. ${guide.purpose} Next: ${guide.next[1]}.`;
    }
  }
}

async function refresh() {
  const recovering = state.hostAvailable === false || !state.security;
  try {
    if (recovering) {
      const context = await refreshSecurityContext();
      if (context.lan_mode && !context.authenticated) {
        enterLoginMode();
        return false;
      }
    }
    let snapshot;
    try {
      snapshot = await api("/api/v1/status");
    } catch (error) {
      if (!(error instanceof ApiError) || ![401, 403].includes(error.status)) throw error;
      const context = await refreshSecurityContext();
      if (context.lan_mode && !context.authenticated) {
        enterLoginMode();
        return false;
      }
      snapshot = await api("/api/v1/status");
    }
    setHostAvailability(true);
    applySnapshot(snapshot);
    if (state.pollTimer && (!state.socket || state.socket.readyState === WebSocket.CLOSED)) {
      connectSocket();
    }
    return true;
  } catch (error) {
    const newlyUnavailable = state.hostAvailable !== false;
    setHostAvailability(false, error);
    startPolling();
    if (newlyUnavailable) showError(error);
    return false;
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
  window.clearTimeout(state.reconnectTimer);
  if (state.security?.lan_mode && !state.security?.authenticated) return;
  if (!navigator.onLine) return;
  if (state.socket && state.socket.readyState < WebSocket.CLOSING) state.socket.close();
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
    try {
      const snapshot = JSON.parse(event.data);
      if (state.hostAvailable === false || !state.security) {
        applySnapshot(snapshot);
        void refresh();
      } else {
        setHostAvailability(true);
        applySnapshot(snapshot);
      }
    } catch {
      socket.close(1003, "invalid snapshot");
    }
  });
  socket.addEventListener("close", () => {
    if (state.socket !== socket) return;
    state.socketFailures += 1;
    startPolling();
    updateChrome();
    const delay = Math.min(15000, 500 * (2 ** Math.min(state.socketFailures, 5)));
    if (navigator.onLine) state.reconnectTimer = window.setTimeout(connectSocket, delay);
  });
  socket.addEventListener("error", () => socket.close());
}

function page(title, subtitle, content) {
  return `<section><h1 tabindex="-1">${escapeHtml(title)}</h1>${subtitle ? `<p class="lead">${escapeHtml(subtitle)}</p>` : ""}${content}</section>`;
}

function demoGuideStorageKey(path) {
  return `${DEMO_GUIDE_STORAGE_PREFIX}${encodeURIComponent(path)}`;
}

function demoGuideIsDismissed(path) {
  if (state.dismissedDemoGuides.has(path)) return true;
  try {
    return sessionStorage.getItem(demoGuideStorageKey(path)) === "true";
  } catch {
    return false;
  }
}

function setDemoGuideDismissed(path, dismissed) {
  if (dismissed) state.dismissedDemoGuides.add(path);
  else state.dismissedDemoGuides.delete(path);
  try {
    if (dismissed) sessionStorage.setItem(demoGuideStorageKey(path), "true");
    else sessionStorage.removeItem(demoGuideStorageKey(path));
  } catch {
    // The in-memory preference keeps the tutorial usable when storage is disabled.
  }
}

function demoGuideMarkup(path) {
  const { key, guide } = demoGuideContext(path);
  if (state.snapshot?.mode !== "demo" || !guide) return "";
  if (demoGuideIsDismissed(key)) {
    return `<div class="demo-guide-launcher">
      <button type="button" class="secondary" data-action="demo-guide-open" data-host-independent="true">
        <span aria-hidden="true">?</span> Show demo guide
      </button>
    </div>`;
  }
  const steps = guide.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("");
  const nextAction = guide.nextAction
    ? ` data-action="${escapeHtml(guide.nextAction)}" data-host-independent="true"`
    : "";
  return `<aside id="demo-guide" class="card demo-guide" data-demo-guide role="region" aria-labelledby="demo-guide-title" tabindex="-1">
    <div class="demo-guide-heading">
      <div>
        <p class="demo-guide-eyebrow">${escapeHtml(guide.eyebrow)}</p>
        <h2 id="demo-guide-title">${escapeHtml(guide.title)}</h2>
      </div>
      <button type="button" class="ghost demo-guide-dismiss" data-action="demo-guide-dismiss" data-host-independent="true" aria-label="Hide the demo guide on this page">Hide tips</button>
    </div>
    <p class="demo-guide-purpose">${escapeHtml(guide.purpose)}</p>
    <ol class="step-list demo-guide-steps">${steps}</ol>
    <nav class="demo-guide-nav" aria-label="Demo tutorial navigation">
      <a id="demo-guide-previous" class="button secondary" href="#${guide.previous[0]}"><span aria-hidden="true">←</span> ${escapeHtml(guide.previous[1])}</a>
      <a id="demo-guide-next" class="button" href="#${guide.next[0]}"${nextAction}>${escapeHtml(guide.next[1])} <span aria-hidden="true">→</span></a>
    </nav>
  </aside>`;
}

function mountDemoGuide() {
  const markup = demoGuideMarkup(route());
  if (!markup) return;
  const slot = `<div class="demo-guide-slot" data-demo-guide-slot>${markup}</div>`;
  const pourScreen = main.querySelector(".pour-screen");
  if (pourScreen) {
    // At kiosk widths the guide sits beside the safety-sensitive live measurement,
    // keeping both the authoritative state and just-in-time instructions visible.
    pourScreen.classList.add("with-demo-guide");
    pourScreen.insertAdjacentHTML("beforeend", slot);
    return;
  }
  const heading = main.querySelector("h1");
  if (!heading) return;
  const insertionPoint = heading.nextElementSibling?.classList.contains("lead")
    ? heading.nextElementSibling
    : heading;
  insertionPoint.insertAdjacentHTML(
    "afterend",
    slot,
  );
}

function refreshDemoGuide(open) {
  const path = route();
  const { key } = demoGuideContext(path);
  setDemoGuideDismissed(key, !open);
  const slot = main.querySelector("[data-demo-guide-slot]");
  if (!slot) return;
  slot.innerHTML = demoGuideMarkup(path);
  const focusTarget = open
    ? slot.querySelector("[data-demo-guide]")
    : slot.querySelector('[data-action="demo-guide-open"]');
  focusTarget?.focus({ preventScroll: true });
}

function focusSelector(element) {
  if (!(element instanceof HTMLElement) || !main.contains(element)) return null;
  if (element.id) return `#${CSS.escape(element.id)}`;
  const owner = element.closest("form[id], form[data-id], form[data-pour]");
  let ownerSelector = "";
  if (owner?.id) ownerSelector = `#${CSS.escape(owner.id)}`;
  else if (owner?.dataset.id) ownerSelector = `form[data-id="${CSS.escape(owner.dataset.id)}"]`;
  else if (owner?.dataset.pour) ownerSelector = `form[data-pour="${CSS.escape(owner.dataset.pour)}"]`;
  if (element.getAttribute("name")) return `${ownerSelector ? `${ownerSelector} ` : ""}[name="${CSS.escape(element.getAttribute("name"))}"]`;
  if (ownerSelector && element.matches("button")) return `${ownerSelector} button:not([type="button"])`;
  if (element.dataset.action) {
    let selector = `[data-action="${CSS.escape(element.dataset.action)}"]`;
    for (const key of ["participant", "pour", "calibration", "ordinal", "purpose"]) {
      if (element.dataset[key] !== undefined) {
        selector += `[data-${key}="${CSS.escape(element.dataset[key])}"]`;
      }
    }
    return selector;
  }
  return null;
}

function currentDevicePhase() {
  return String(state.snapshot?.device?.status?.state || "unknown").toLowerCase();
}

function armIsAvailable() {
  return state.hostAvailable !== false
    && state.snapshot?.connection?.state === "connected"
    && currentDevicePhase() === "idle"
    && !state.snapshot?.session
    && !state.snapshot?.pending_capture;
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
  const devicePhase = currentDevicePhase();
  const unattributedFlow = !s.session && ["pouring", "settling"].includes(devicePhase);
  const readyToArm = armIsAvailable();
  const phaseLabel = devicePhase === "unknown"
    ? "Unknown"
    : `${devicePhase.charAt(0).toUpperCase()}${devicePhase.slice(1).replaceAll("_", " ")}`;
  const deviceDetail = state.hostAvailable === false
    ? "The host cannot confirm current hardware state. This is the last received phase."
    : s.connection?.state !== "connected"
      ? `The flow device is ${s.connection?.state || "offline"}; new pours cannot be armed.`
      : unattributedFlow
        ? "Unattributed flow is being counted from raw device pulses. New participant selection is blocked until this event finishes."
        : devicePhase === "idle"
          ? "Connected and ready to arm a participant or Guest pour."
          : `The device is ${devicePhase.replaceAll("_", " ")}; new participant selection is blocked.`;
  const title = unattributedFlow ? "Unattributed flow in progress" : readyToArm ? "Ready for a pour?" : "KegPulse is not ready to arm";
  const subtitle = unattributedFlow
    ? "The tap opened without a selected participant. KegPulse is preserving the measurement."
    : readyToArm ? "Select a person before opening the tap, or choose Guest." : "Review the visible device state before opening the tap.";
  const armDisabled = state.pending.has("arm") || !readyToArm;
  const buttons = participants.map((participant) => `
    <button class="participant-button" data-action="arm" data-participant="${escapeHtml(participant.id)}" aria-describedby="home-device-detail" ${armDisabled ? "disabled" : ""}>
      ${escapeHtml(participant.display_name)}
    </button>`).join("");
  return page(title, subtitle, `
    ${warnings.length ? `<aside class="card setup-callout" aria-labelledby="setup-title"><h2 id="setup-title">Setup and review</h2><ul>${warnings.join("")}</ul></aside>` : ""}
    <section class="card device-phase-card ${unattributedFlow ? "active" : ""}" aria-labelledby="home-device-title">
      <h2 id="home-device-title">Flow device</h2>
      <div class="device-phase">${escapeHtml(phaseLabel)}</div>
      <p id="home-device-detail">${escapeHtml(deviceDetail)}</p>
    </section>
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
        <button class="participant-button guest" data-action="arm" data-participant="" aria-describedby="home-device-detail" ${armDisabled ? "disabled" : ""}>${participants.length === 0 ? "Start pour" : "Guest / Unattributed"}</button>
      </div>
      ${participants.length === 0 ? '<p class="muted">No profiles yet. “Start pour” records an unattributed event; you can assign it later.</p>' : ""}
    </section>
  `);
}

function pourView() {
  const s = state.snapshot;
  const session = s.session || s.pending_capture || s.terminal_notice;
  if (!session) return page("No active pour", "The device has no active session.", '<a class="button" href="#/">Return home</a>');
  const participant = s.participants.find((item) => item.id === session.participant_id);
  const purpose = session.purpose || "pour";
  const terminalStatus = ["complete", "timed_out", "interrupted_uncertain"].includes(session.status);
  const status = terminalStatus ? session.status : (s.device?.status?.state || session.status);
  const volume = s.live_volume_ml;
  const pulses = s.device?.status?.pulses ?? session.captured_raw_pulses ?? 0;
  const afterFlow = decimal(pulses) > 0 || ["pouring", "settling", "finalizing", "complete"].includes(status);
  const title = purpose === "calibration"
    ? `Calibration sample ${session.target_ordinal}`
    : purpose === "verification" ? "Verification pour"
      : status === "timed_out" ? "Arming timed out"
        : status === "interrupted_uncertain" ? "Connection interrupted"
        : (participant?.display_name || "Guest / Unattributed");
  const cancelLabel = afterFlow ? "End and save partial pour" : "Cancel arming";
  const armLeft = Math.max(0, decimal(s.device?.status?.arm_left));
  const countdown = status === "armed"
    ? `${Math.ceil(armLeft / 1000)} seconds left to open the tap`
    : `${pulses} raw pulses`;
  const note = status === "armed" ? "Open the tap before the arming window expires." :
    status === "pouring" ? "Flow detected. Raw pulses are being counted on the device." :
    status === "settling" ? "Flow paused. You may briefly resume before completion." :
    status === "complete" ? "Measurement captured. Enter the scale mass to continue." :
    status === "timed_out" ? "No pulse arrived before the deadline, so no pour was recorded." :
    status === "interrupted_uncertain" ? "The device became unavailable before the host could confirm the outcome. No pulses were invented or discarded; review diagnostics and history after reconnecting." :
    "Waiting for the authoritative device state.";
  return `<section class="pour-screen"><div class="pour-panel card">
    <p class="pour-state">${escapeHtml(status)}</p>
    <h1 tabindex="-1">${escapeHtml(title)}</h1>
    ${["timed_out", "interrupted_uncertain"].includes(status) ? `<div class="pour-amount">${status === "timed_out" ? "No flow" : "Needs review"}</div>` : `<output class="pour-amount" aria-label="Measured pour volume">${formatVolume(volume)}</output>`}
    <p class="countdown" aria-live="polite">${escapeHtml(countdown)}</p>
    <p>${escapeHtml(note)}</p>
    ${["timed_out", "interrupted_uncertain"].includes(status) ? '<button data-action="dismiss-terminal">Return home</button>' : status === "complete" && purpose !== "pour" ? '<a class="button" href="#/calibration">Enter scale mass</a>' : `
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
      <p id="return-countdown" class="muted">${state.completionPaused ? "Auto-return paused." : "Returning home shortly. Interaction pauses auto-return."}</p>
      <div class="button-row"><button data-action="home-now">Return home</button><button class="secondary" data-action="stay">Stay here</button></div>
    </section>`);
}

function scheduleCompletionReturn() {
  window.clearTimeout(state.completionTimer);
  if (state.completionPaused) return;
  const seconds = Number(state.snapshot?.settings?.completion_seconds ?? 9);
  if (seconds > 0) state.completionTimer = window.setTimeout(() => navigate("/"), seconds * 1000);
}

function pauseCompletionReturn() {
  state.completionPaused = true;
  window.clearTimeout(state.completionTimer);
  const message = document.querySelector("#return-countdown");
  if (message) message.textContent = "Auto-return paused.";
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
  const action = (row) => row.participant_id
    ? "Assigned"
    : state.reassignPourId === row.id
      ? reassignmentEditor(row)
      : `<button class="secondary" data-action="show-reassign" data-pour="${escapeHtml(row.id)}">Assign</button>`;
  return `<div class="table-wrap"><table><thead><tr><th>When</th><th>Person</th><th>Amount</th><th>Evidence</th><th>Action</th></tr></thead><tbody>${rows.map((row) => `
    <tr><td>${formatTime(row.ended_at)}</td><td>${escapeHtml(row.participant_name || "Guest / Unattributed")}</td><td>${formatVolume(row.volume_ml)}</td><td>${escapeHtml(row.raw_pulses)} pulses<br>${escapeHtml(row.quality.replaceAll("_", " "))}${pourDetails(row)}</td><td>${action(row)}</td></tr>`).join("")}</tbody></table></div>
    <div class="sample-cards">${rows.map((row) => `<article class="sample-card"><strong>${formatVolume(row.volume_ml)}</strong><br>${escapeHtml(row.participant_name || "Guest / Unattributed")}<br><span class="muted">${formatTime(row.ended_at)} · ${escapeHtml(row.raw_pulses)} pulses · ${escapeHtml(row.quality.replaceAll("_", " "))}</span>${pourDetails(row)}${action(row)}</article>`).join("")}</div>`;
}

function pourDetails(row) {
  return `<details class="pour-details"><summary>Measurement details</summary><dl class="status-list">
    <dt>Started</dt><dd>${formatTime(row.started_at)}</dd><dt>Ended</dt><dd>${formatTime(row.ended_at)}</dd>
    <dt>Keg</dt><dd>${escapeHtml(row.keg_label || row.keg_id || "Not assigned")}</dd>
    <dt>Calibration</dt><dd>${escapeHtml(row.calibration_id || "No active calibration")}</dd>
    <dt>Device</dt><dd>${escapeHtml(row.device_id)} / ${escapeHtml(row.boot_id)}</dd>
    <dt>Event</dt><dd>${escapeHtml(row.event_seq ?? "Recovered counter evidence")}</dd>
    <dt>Fault</dt><dd>${escapeHtml(row.fault || "none")}</dd>
  </dl></details>`;
}

function reassignmentEditor(row) {
  const options = (state.snapshot.participants || []).map((participant) =>
    `<option value="${escapeHtml(participant.id)}">${escapeHtml(participant.display_name)}</option>`
  ).join("");
  return `<form class="reassign-form stack" data-pour="${escapeHtml(row.id)}">
    <label>Assign participant<select name="participant_id" required><option value="">Choose a person</option>${options}</select></label>
    <label>Reason<input name="reason" maxlength="500" value="Confirmed by administrator" required></label>
    <div class="button-row"><button>Review assignment</button><button type="button" class="secondary" data-action="cancel-reassign">Cancel</button></div>
  </form>`;
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
        <form id="keg-form" class="stack"><label>Label<input name="label" maxlength="120" required autocomplete="off"></label><label>Starting volume (mL)<input name="starting_volume_ml" type="number" inputmode="decimal" min="1" max="200000" step="0.1" required></label><label>Installed at<input name="installed_at" type="datetime-local" step="0.001" value="${localDateTimeInput()}" required><span class="field-help">Recorded with this kiosk's time zone and stored in UTC.</span></label><label>Notes (optional)<textarea name="notes" maxlength="1000"></textarea></label><button>${keg ? "Review and replace" : "Install keg"}</button></form>
      </section>
    </div>
    ${keg ? `<section class="card"><h2>Manual inventory adjustment</h2><form id="adjustment-form" class="grid two"><label>Signed amount (mL)<input name="amount_ml" type="number" inputmode="decimal" step="0.1" min="-200000" max="200000" required><span class="field-help">Positive adds inventory; negative removes it.</span></label><label>Reason<input name="reason" maxlength="500" required></label><button>Review adjustment</button></form></section>` : ""}
  `);
}

function sampleReview(detail) {
  const analysis = detail.analysis;
  const editable = detail.status === "draft";
  const rows = detail.samples.map((sample, index) => {
    const a = analysis?.samples?.[index];
    const flagged = a?.suspected_outlier || sample.suspected_outlier;
    const consistency = !sample.included
      ? flagged ? "Excluded — suspected outlier" : "Excluded by user"
      : flagged ? "Suspected outlier" : "Consistent";
    const predicted = a ? `${Number(a.predicted_volume_ml).toFixed(2)} mL` : "—";
    const residual = a ? `${Number(a.residual_ml).toFixed(2)} mL (${Number(a.percentage_error).toFixed(2)}%)` : "—";
    const action = editable
      ? `<td><button class="secondary" data-action="toggle-sample" data-calibration="${escapeHtml(detail.id)}" data-ordinal="${sample.ordinal}" data-included="${sample.included ? "0" : "1"}">${sample.included ? "Exclude" : "Include"}</button></td>`
      : "";
    return `<tr class="${flagged ? "outlier" : !sample.included ? "excluded" : ""}"><td>${sample.ordinal}</td><td>${sample.raw_pulses}</td><td>${sample.mass_g} g</td><td>${Number(sample.derived_volume_ml).toFixed(2)} mL</td><td>${predicted}</td><td>${residual}</td><td>${consistency}</td>${action}</tr>`;
  }).join("");
  const cards = detail.samples.map((sample, index) => {
    const a = analysis?.samples?.[index];
    const flagged = a?.suspected_outlier || sample.suspected_outlier;
    const consistency = !sample.included
      ? flagged ? "Excluded — suspected outlier" : "Excluded by user"
      : flagged ? "Suspected outlier" : "Consistent";
    const consistencyClass = flagged ? "warning-text" : sample.included ? "good-text" : "muted";
    const action = editable
      ? `<div><button class="secondary" data-action="toggle-sample" data-calibration="${escapeHtml(detail.id)}" data-ordinal="${sample.ordinal}" data-included="${sample.included ? "0" : "1"}">${sample.included ? "Exclude" : "Include"}</button></div>`
      : "";
    return `<article class="sample-card ${flagged ? "outlier" : !sample.included ? "excluded" : ""}"><strong>Sample ${sample.ordinal}</strong><p>${sample.raw_pulses} pulses · ${sample.mass_g} g</p><dl class="sample-metrics"><dt>Actual scale volume</dt><dd>${Number(sample.derived_volume_ml).toFixed(2)} mL</dd><dt>Predicted volume</dt><dd>${a ? `${Number(a.predicted_volume_ml).toFixed(2)} mL` : "—"}</dd><dt>Residual / error</dt><dd>${a ? `${Number(a.residual_ml).toFixed(2)} mL (${Number(a.percentage_error).toFixed(2)}%)` : "—"}</dd></dl><p class="${consistencyClass}">${consistency}</p>${action}</article>`;
  }).join("");
  const guidance = editable
    ? "Suspected outliers remain included until you decide."
    : `This ${escapeHtml(detail.status)} calibration is read-only; its inclusion decisions are preserved.`;
  const analysisSummary = analysis
    ? `<p><strong>Aggregate factor:</strong> ${Number(analysis.pulses_per_ml).toFixed(6)} pulses/mL · variation ${Number(analysis.coefficient_of_variation_pct).toFixed(2)}%</p>${editable ? `<button data-action="activate-calibration" data-calibration="${escapeHtml(detail.id)}">Review and activate</button>` : ""}`
    : editable
      ? '<p class="muted">Capture all ten samples and keep at least seven included to activate.</p>'
      : "";
  return `<section class="card"><h2>Sample review</h2><p>${detail.samples.length}/10 captured · ${analysis?.included_count ?? detail.samples.filter((x) => x.included).length} included. ${guidance}</p>
    <div class="table-wrap"><table><thead><tr><th>#</th><th>Pulses</th><th>Mass</th><th>Actual scale volume</th><th>Predicted volume</th><th>Residual / error</th><th>Consistency</th>${editable ? "<th>Use sample</th>" : ""}</tr></thead><tbody>${rows}</tbody></table></div><div class="sample-cards">${cards}</div>
    ${analysisSummary}
  </section>`;
}

function calibrationView() {
  const active = state.snapshot.active_calibration;
  const capture = state.snapshot.pending_capture;
  const verification = state.snapshot.last_verification;
  const captureDensity = capture?.density_g_per_ml
    || state.calibrationDetails?.find((item) => item.id === capture?.calibration_id)?.default_density_g_per_ml
    || "1.000";
  return page("Calibration & verification", "Use a tared scale. Mass ÷ density gives volume; KegPulse uses total pulses ÷ total volume.", `
    <section class="card setup-callout"><h2>Ten-pour procedure</h2><ol class="step-list"><li>Tare an empty glass on the scale.</li><li>Use water at 1.000 g/mL first, then repeat with the installed keg and known/approximate beer density.</li><li>Capture ten varied-size pours; enter the scale mass after each.</li><li>Review residuals and explicitly include or exclude suspected outliers.</li><li>Activate only after reviewing the aggregate factor.</li></ol><p class="warning-text">Density directly affects volume. KegPulse is not a legal-for-trade meter.</p></section>
    ${capture?.status === "complete" ? `<section class="card"><h2>${capture.purpose === "verification" ? "Enter verification mass" : `Enter mass for sample ${capture.target_ordinal}`}</h2><p>${escapeHtml(capture.captured_raw_pulses)} raw pulses captured. Selected density: <strong>${escapeHtml(captureDensity)} g/mL</strong>.</p><form id="capture-commit-form" data-purpose="${capture.purpose}" data-session="${capture.session_id}" data-calibration="${capture.calibration_id || ""}" class="grid two"><label>Scale mass (g)<input name="mass_g" type="number" inputmode="decimal" min="0.1" max="10000" step="0.01" required></label><label>Density (g/mL)<input name="density_g_per_ml" type="number" inputmode="decimal" min="0.5" max="2" step="0.001" value="${escapeHtml(captureDensity)}" required></label>${capture.purpose === "calibration" ? '<label><input name="included" type="checkbox" checked> Include this sample</label>' : ""}<button>Save measured check</button></form></section>` : ""}
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
  return details.map((detail) => `<article class="card" data-calibration-status="${escapeHtml(detail.status)}"><h2>${escapeHtml(detail.liquid)} · ${escapeHtml(detail.status)}</h2><p>Created ${formatTime(detail.created_at)}</p>${detail.status === "draft" && detail.samples.length < 10 ? `<button data-action="capture-sample" data-calibration="${escapeHtml(detail.id)}" data-ordinal="${detail.samples.length + 1}">Capture sample ${detail.samples.length + 1}</button>` : ""}${detail.samples.length ? sampleReview(detail) : '<p class="empty">No samples captured yet.</p>'}</article>`).join("");
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
  const portOptions = (state.serialPorts || []).map((port) => `<option value="${escapeHtml(port.device)}">${escapeHtml(port.description)}</option>`).join("");
  const diagnosticRows = state.diagnostics === null
    ? '<button data-action="load-diagnostics" class="secondary">Load recent diagnostics</button>'
    : state.diagnostics.length
      ? `<ol class="diagnostic-list">${state.diagnostics.map((item) => `<li><strong>${escapeHtml(item.level)} · ${escapeHtml(item.code)}</strong><br><span class="muted">${formatTime(item.created_at)}</span><br><code>${escapeHtml(JSON.stringify(item.context || {}))}</code></li>`).join("")}</ol>`
      : '<p class="empty">No recent diagnostics.</p>';
  const timingSource = s.mode === "demo"
    ? "host simulator setting"
    : "host reference only; actual firmware value is compile-time and not reported by KP1";
  return page("Device & settings", "Hardware state and recovery information stay visible. LAN mode is configured offline and requires a PIN.", `
    ${s.settings?.lan_mode ? '<aside class="banner warning" role="status"><strong>Trusted-LAN mode is active.</strong> Traffic is plain HTTP on the trusted network; an administrator PIN and exact allowlists are required.</aside>' : ""}
    ${s.mode === "demo" ? demoPanel() : ""}
    <div class="grid two"><section class="card"><h2>Flow device</h2><dl class="status-list"><dt>Connection</dt><dd>${escapeHtml(s.connection.state)} — ${escapeHtml(s.connection.detail)}</dd><dt>Protocol</dt><dd>${escapeHtml(device.identity.proto || "—")}</dd><dt>Firmware</dt><dd>${escapeHtml(device.identity.fw || "—")}</dd><dt>Device ID</dt><dd>${escapeHtml(device.identity.device || "—")}</dd><dt>Boot ID</dt><dd>${escapeHtml(device.identity.boot || "—")}</dd><dt>State</dt><dd>${escapeHtml(device.status.state || "—")}</dd><dt>Lifetime pulses</dt><dd>${escapeHtml(device.status.lifetime || "0")}</dd><dt>Recovered pulses</dt><dd>${escapeHtml(device.counters?.recovery || "0")}</dd><dt>Device fault</dt><dd>${escapeHtml(device.counters?.fault || "none")}</dd><dt>Rejected noise edges</dt><dd>${escapeHtml(device.counters?.rejected || "0")}</dd><dt>Noise gate</dt><dd>${escapeHtml(device.counters?.noise_gate_us || "0")} µs</dd><dt>Host flow-gap default</dt><dd>${escapeHtml(s.settings.flow_gap_ms || "—")} ms (${escapeHtml(timingSource)})</dd><dt>Host settling default</dt><dd>${escapeHtml(s.settings.settling_ms || "—")} ms (${escapeHtml(timingSource)})</dd><dt>Queue overflows</dt><dd>${escapeHtml(s.connection.queue_overflows)}</dd></dl><div class="button-row"><button data-action="load-ports" class="secondary">Scan serial ports</button>${s.mode === "hardware" ? '<button data-action="serial-reconnect" class="secondary">Reconnect device</button>' : ""}</div><div id="port-results">${state.serialPorts === null ? "" : state.serialPorts.length ? `<ul>${state.serialPorts.map((p) => `<li>${escapeHtml(p.device)} — ${escapeHtml(p.description)}</li>`).join("")}</ul>` : '<p class="empty">No serial ports detected.</p>'}</div></section>
    <section class="card"><h2>Display & timing</h2><form id="settings-form" class="stack"><label>Units<select name="display_units"><option value="us_fl_oz" ${s.settings.display_units === "us_fl_oz" ? "selected" : ""}>US fl oz</option><option value="ml" ${s.settings.display_units === "ml" ? "selected" : ""}>mL</option><option value="l" ${s.settings.display_units === "l" ? "selected" : ""}>Liters</option></select></label><label>Completion display (seconds)<input name="completion_seconds" type="number" min="0" max="60" value="${escapeHtml(s.settings.completion_seconds)}"></label><label>Arming timeout (milliseconds)<input name="arm_timeout_ms" type="number" min="1000" max="120000" step="100" value="${escapeHtml(s.settings.arm_timeout_ms)}"></label><label>Verification warning (%)<input name="verification_warning_pct" type="number" min="0.1" max="100" step="0.1" value="${escapeHtml(s.settings.verification_warning_pct)}"></label>${s.mode === "hardware" ? `<label>Preferred serial port<input name="serial_port" list="serial-port-options" maxlength="260" value="${escapeHtml(s.settings.serial_port || "")}" placeholder="Auto-detect after handshake"><span class="field-help">Choose a scanned port or enter a COM or /dev path. Save, then reconnect.</span></label><datalist id="serial-port-options">${portOptions}</datalist>` : ""}<button>Save settings</button></form></section></div>
    <div class="grid two"><section class="card"><h2>Administrator PIN</h2><p>${state.security?.pin_configured ? "A PIN protects administrative actions." : "No PIN is configured. Anyone with physical access to this loopback kiosk can administer it."}</p><p id="admin-auth-status" class="${state.security?.authenticated ? "good-text" : "warning-text"}" role="status">${state.security?.authenticated ? "Administrator unlocked for this session." : "Administrator locked."}</p><form id="pin-form" class="stack"><label>${state.security?.pin_configured ? "New PIN" : "PIN"}<input name="pin" type="password" inputmode="numeric" minlength="6" maxlength="20" pattern="[0-9]+" autocomplete="new-password" required></label><button>${state.security?.pin_configured ? "Change PIN" : "Set PIN"}</button></form><div id="admin-login-slot">${state.security?.pin_configured && !state.security?.authenticated ? loginFormMarkup(true) : ""}</div></section>
    <section class="card"><h2>Data & privacy</h2><p>Database, rotating logs, backups, and exports remain on this device. Backups are not encrypted; store them securely.</p><button data-action="backup">Create atomic backup</button><a class="button secondary" href="/api-docs">Local API schema</a><p>Network mode: <strong>${s.settings?.lan_mode ? "trusted LAN" : "loopback only"}</strong>. No telemetry or cloud dependency.</p></section></div>
    <section class="card"><h2>Recent device diagnostics</h2><p>Bounded local recovery and protocol events; routine personal pour history is not logged here.</p>${diagnosticRows}</section>
  `);
}

function demoPanel() {
  return `<section id="demo-simulator-controls" class="card" aria-labelledby="demo-title" tabindex="-1"><h2 id="demo-title">Demo simulator controls</h2><p class="warning-text">Demo mode is explicit. These controls do not exist in hardware mode.</p><div class="button-row"><button data-action="demo-pulse" data-count="25">Add 25 pulses</button><button data-action="demo-finish">Finish pour</button><button class="secondary" data-action="demo-disconnect">Disconnect</button><button class="secondary" data-action="demo-reconnect">Reconnect</button><button class="danger" data-action="demo-reset">Reset device</button></div><fieldset><legend>Next-frame fault</legend><div class="button-row"><button class="secondary" data-action="demo-fault" data-fault="corrupt_next">Corrupt</button><button class="secondary" data-action="demo-fault" data-fault="duplicate_next">Duplicate</button><button class="secondary" data-action="demo-fault" data-fault="delay_next">Delay</button><button class="secondary" data-action="demo-flush">Flush delayed</button></div></fieldset></section>`;
}

function loginView() {
  return page("Administrator login", "This device requires a PIN before local data and controls are shown.", `<section class="card">${loginFormMarkup()}</section>`);
}

function render() {
  const priorFocus = focusSelector(document.activeElement);
  const priorSelection = document.activeElement instanceof HTMLInputElement
    ? [document.activeElement.selectionStart, document.activeElement.selectionEnd]
    : null;
  if (!state.snapshot) {
    if (state.hostAvailable === false) {
      const message = state.hostError instanceof Error ? state.hostError.message : "Waiting for the local service.";
      main.innerHTML = page("KegPulse service unavailable", "The browser cannot confirm hardware or data while the local service is down.", `<section class="card"><p>${escapeHtml(message)}</p><button data-action="retry">Try again</button></section>`);
      bindForms();
      main.querySelector("h1")?.focus({ preventScroll: true });
      state.renderedRoute = "__unavailable__";
    } else if (state.security?.lan_mode && !state.security?.authenticated) {
      main.innerHTML = loginView();
      bindForms();
      main.querySelector("h1")?.focus({ preventScroll: true });
      state.renderedRoute = "__login__";
    }
    syncHostControls();
    return;
  }
  const loginRequired = state.snapshot.settings?.lan_mode && !state.security?.authenticated;
  const renderKey = loginRequired ? "__login__" : route();
  const shouldFocus = state.renderedRoute !== renderKey;
  if (loginRequired) {
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
  mountDemoGuide();
  bindForms();
  if (shouldFocus) {
    main.querySelector("h1")?.focus({ preventScroll: true });
  } else if (priorFocus) {
    const candidates = [...main.querySelectorAll(priorFocus)];
    const replacement = candidates.find((item) => item.offsetParent !== null) || candidates[0];
    replacement?.focus({ preventScroll: true });
    if (replacement instanceof HTMLInputElement && priorSelection && priorSelection[0] !== null) {
      replacement.setSelectionRange(priorSelection[0], priorSelection[1]);
    }
  }
  state.renderedRoute = renderKey;
  syncHostControls();
}

async function arm(participantId) {
  if (!armIsAvailable()) {
    showError("The flow device is not idle and ready. Wait for the visible device state to recover before arming.");
    return;
  }
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
  state.reassignPourId = pourId;
  render();
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
  try {
    await mutation(`demo-${action}`, "/api/v1/demo/action", { action, ...values });
  } catch (error) { showError(error); }
}

function bindLoginForm() {
  document.querySelector("#login-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try {
      state.security = await api("/api/v1/security/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin: form.get("pin") }) });
      await refresh();
      syncSecurityUi();
      if (!state.socket) connectSocket();
      showToast("Administrator unlocked");
    } catch (error) { showError(error); }
  });
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
    try { await mutation("keg", "/api/v1/kegs/replace", { label: form.get("label"), starting_volume_ml: form.get("starting_volume_ml"), installed_at: new Date(form.get("installed_at")).toISOString(), notes: form.get("notes") }); showToast("Keg installed"); } catch (error) { showError(error); }
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
    const payload = { display_units: form.get("display_units"), completion_seconds: Number(form.get("completion_seconds")), arm_timeout_ms: Number(form.get("arm_timeout_ms")), verification_warning_pct: form.get("verification_warning_pct") };
    if (form.has("serial_port")) {
      const serialPort = String(form.get("serial_port") || "").trim();
      payload.serial_port = serialPort || null;
    }
    try { const result = await mutation("settings", "/api/v1/settings", payload, "PATCH"); showToast(result?.serial_reconnect_required ? "Settings saved. Reconnect the device to apply the selected port." : "Settings saved"); } catch (error) { showError(error); }
  });
  document.querySelector("#pin-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try { await mutation("pin", "/api/v1/security/pin", { pin: form.get("pin") }, "PUT"); state.security = await api("/api/v1/security/context"); showToast("Administrator PIN updated; unlock again with the new PIN"); render(); } catch (error) { showError(error); }
  });
  bindLoginForm();
  for (const form of document.querySelectorAll(".participant-edit")) form.addEventListener("submit", async (event) => {
    event.preventDefault(); const element = event.currentTarget; const data = new FormData(element);
    try { await mutation(`profile-${element.dataset.id}`, `/api/v1/participants/${element.dataset.id}`, { display_name: data.get("display_name"), active: data.get("active") !== null }, "PATCH"); await loadParticipants(); } catch (error) { showError(error); }
  });
  for (const form of document.querySelectorAll(".reassign-form")) form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const element = event.currentTarget;
    const data = new FormData(element);
    const participant = element.querySelector("select")?.selectedOptions[0]?.textContent || "the selected participant";
    const accepted = await confirmAction(`Assign this measured pour to ${participant}? Volume and timestamps will not change.`, "Assign pour");
    if (!accepted) return;
    try {
      await mutation(`assign-${element.dataset.pour}`, `/api/v1/history/${element.dataset.pour}/reassign`, { participant_id: data.get("participant_id"), reason: data.get("reason") });
      state.reassignPourId = null;
      await loadHistory();
    } catch (error) { showError(error); }
  });
}

async function loadParticipants() {
  try { state.participantDetails = await api("/api/v1/participants?include_inactive=true"); render(); } catch (error) { showError(error); }
}

main.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]"); if (!button) return;
  const action = button.dataset.action;
  if (action === "retry") return window.location.reload();
  if (action === "demo-guide-dismiss") { refreshDemoGuide(false); return; }
  if (action === "demo-guide-open") { refreshDemoGuide(true); return; }
  if (action === "demo-guide-controls") {
    event.preventDefault();
    const controls = main.querySelector("#demo-simulator-controls");
    controls?.scrollIntoView({ behavior: "smooth", block: "start" });
    controls?.focus({ preventScroll: true });
    return;
  }
  if (action === "dismiss-terminal") {
    state.dismissedTerminalId = state.snapshot?.terminal_notice?.session_id || "dismissed";
    return navigate("/");
  }
  if (action === "arm") return arm(button.dataset.participant);
  if (action === "cancel") return cancelPour();
  if (action === "home-now") { state.completionPaused = false; return navigate("/"); }
  if (action === "stay") { pauseCompletionReturn(); return; }
  if (action === "load-history") return loadHistory();
  if (action === "show-reassign") return showReassign(button.dataset.pour);
  if (action === "cancel-reassign") { state.reassignPourId = null; render(); return; }
  if (action === "load-calibrations") return loadCalibrations();
  if (action === "capture-sample") return captureSample(button.dataset.calibration, button.dataset.ordinal);
  if (action === "toggle-sample") { try { await mutation(`sample-${button.dataset.ordinal}`, `/api/v1/calibrations/${button.dataset.calibration}/samples/${button.dataset.ordinal}`, { included: button.dataset.included === "1" }, "PATCH"); await loadCalibrations(); } catch (error) { showError(error); } return; }
  if (action === "activate-calibration") { const accepted = await confirmAction("Activate this reviewed factor? Historical pours will keep their original calibration.", "Activate calibration"); if (accepted) { try { await mutation("activate", `/api/v1/calibrations/${button.dataset.calibration}/activate`); showToast("Calibration activated"); await loadCalibrations(); } catch (error) { showError(error); } } return; }
  if (action === "start-verification") return startVerification();
  if (action === "load-participants") return loadParticipants();
  if (action === "load-ports") { try { state.serialPorts = await api("/api/v1/serial/ports"); render(); } catch (error) { showError(error); } return; }
  if (action === "serial-reconnect") { try { await mutation("serial-reconnect", "/api/v1/serial/reconnect"); showToast("Device reconnect requested"); } catch (error) { showError(error); } return; }
  if (action === "load-diagnostics") { try { state.diagnostics = await api("/api/v1/diagnostics?limit=100"); render(); } catch (error) { showError(error); } return; }
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
    pauseCompletionReturn();
  }
});
main.addEventListener("change", (event) => {
  if (event.target.matches("#history-filter")) loadHistory();
});
document.addEventListener("pointerdown", () => { if (route() === "/complete") pauseCompletionReturn(); }, { passive: true });

menuButton.addEventListener("click", () => {
  const open = !nav.classList.contains("open");
  nav.classList.toggle("open", open);
  menuButton.setAttribute("aria-expanded", String(open));
});
nav.addEventListener("click", () => { nav.classList.remove("open"); menuButton.setAttribute("aria-expanded", "false"); });
window.addEventListener("hashchange", () => {
  state.completionPaused = false;
  render();
  updateChrome();
  if (route() === "/history") void loadHistory();
});
window.addEventListener("offline", () => {
  setHostAvailability(false, new Error("The kiosk network connection is offline."));
  startPolling();
  if (state.socket && state.socket.readyState < WebSocket.CLOSING) state.socket.close();
});
window.addEventListener("online", () => { void refresh(); });

async function initialize() {
  try {
    state.security = await refreshSecurityContext();
    if (state.security.lan_mode && !state.security.authenticated) {
      enterLoginMode();
    } else {
      const ready = await refresh();
      if (ready) {
        if (route() === "/history") await loadHistory();
        connectSocket();
      }
    }
  } catch (error) {
    setHostAvailability(false, error);
    startPolling();
  }
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js").catch(() => {});
}

initialize();
