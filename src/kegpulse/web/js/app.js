const main = document.querySelector("#main");
const badge = document.querySelector("#connection-badge");
const kegSummary = document.querySelector("#keg-summary");
const kegBattery = document.querySelector("#keg-battery");
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
  management: null,
  cameraStream: null,
  cameraLastCapture: 0,
  cameraUploadActive: false,
  cameraStatus: "Camera not armed",
  videoRecorder: null,
  videoChunks: [],
  videoSessionId: null,
  videoSawFlow: false,
  cameraTesting: false,
  cameraAutoArmAt: 0,
  cameraAutoArming: false,
  cancelledSessionId: null,
  boardTab: "keg",
  boardPours: null,
  boardPoursAt: 0,
  boardPoursForLastPour: null,
  boardLastInteraction: 0,
  diagnostics: null,
  reassignPourId: null,
  lastAnnouncedPulses: null,
  lastMeasurementAnnouncement: 0,
  flowRateMlMin: 0,
  lastFlowPulseAt: 0,
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

const MAX_DISPLAY_VOLUME_ML = 1_000_000;
const displayVolume = (value) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) && Math.abs(parsed) <= MAX_DISPLAY_VOLUME_ML
    ? parsed
    : null;
};

const formatVolume = (ml, units = state.snapshot?.settings?.display_units || "us_fl_oz") => {
  if (ml === null || ml === undefined || ml === "") return "Unknown volume";
  const value = displayVolume(ml);
  if (value === null) return "Reading unavailable";
  if (units === "ml") return `${Math.round(value)} mL`;
  if (units === "l") return `${(value / 1000).toFixed(2)} L`;
  return `${(value / 29.5735295625).toFixed(1)} fl oz`;
};

const pourMeasurements = (ml, density = state.snapshot?.active_calibration?.default_density_g_per_ml) => {
  if (ml === null || ml === undefined || ml === "") return null;
  const volumeMl = displayVolume(ml);
  const densityValue = Number(density);
  if (volumeMl === null || volumeMl < 0) return null;
  return {
    ml: volumeMl.toFixed(1),
    flOz: (volumeMl / 29.5735295625).toFixed(1),
    grams: Number.isFinite(densityValue) && densityValue > 0
      ? (volumeMl * densityValue).toFixed(1)
      : null,
  };
};

const pourMeasurementText = (ml, density) => {
  const values = pourMeasurements(ml, density);
  if (!values) return "Unknown volume";
  return `${values.flOz} fl oz · ${values.ml} mL${values.grams ? ` · ~${values.grams} g` : ""}`;
};

const pourMeasurementMarkup = (ml, density, element = "div") => {
  const values = pourMeasurements(ml, density);
  if (!values) return `<${element} class="pour-amount">Unknown volume</${element}>`;
  return `<${element} class="measurement-trio" aria-label="${values.flOz} US fluid ounces, ${values.ml} milliliters${values.grams ? `, approximately ${values.grams} grams` : ""}">
    <span class="measurement-primary"><strong>${values.flOz}</strong><small>US fl oz</small></span>
    <span><strong>${values.ml}</strong><small>mL</small></span>
    ${values.grams ? `<span><strong>~${values.grams}</strong><small>estimated g</small></span>` : ""}
  </${element}>`;
};

const formatFlowRate = (mlPerMinute, units = state.snapshot?.settings?.display_units || "us_fl_oz") => {
  const value = Math.max(0, decimal(mlPerMinute));
  if (units === "ml") return { value: value.toFixed(0), unit: "mL/min" };
  if (units === "l") return { value: (value / 1000).toFixed(2), unit: "L/min" };
  return { value: (value / 29.5735295625).toFixed(1), unit: "fl oz/min" };
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
  if (
    message.toLowerCase().includes("administrator login required")
    && state.security?.pin_configured
    && !state.security?.authenticated
  ) {
    openKeypad(null);
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const method = options.method || "GET";
  if (!["GET", "HEAD"].includes(method)) {
    if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
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
      if (lanLoginRequired(context)) {
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
  return `<form id="login-form" class="stack" data-pin-form><span class="field-label">${label}</span><button type="button" class="pin-display" data-action="open-keypad" aria-label="${label}">Tap to enter PIN</button><input type="hidden" name="pin" autocomplete="off"><button>${button}</button></form>`;
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

function lanLoginRequired(context) {
  // LAN mode guards the network boundary, not this machine. The physical kiosk
  // (loopback) and read-only display viewers always see the app; only a strict
  // remote client is sent to the login page.
  const security = context || state.security;
  if (!security?.lan_mode || security?.authenticated) return false;
  if (security?.local_client || security?.lan_display) return false;
  return true;
}

function connectionText() {
  if (state.hostAvailable === false) return ["bad", "■ Host unavailable"];
  if (lanLoginRequired()) {
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
  kegSummary.textContent = keg && inventory
    ? `≈${beersLeft(inventory.remaining_ml)} beers left · ${formatVolume(inventory.remaining_ml)}`
    : keg
      ? keg.label
      : "No keg configured";
  if (keg && inventory) {
    const rawPercent = Number(inventory.percent_remaining);
    const percent = Number.isFinite(rawPercent) ? Math.max(0, Math.min(100, rawPercent)) : 0;
    const rounded = Math.round(percent);
    kegBattery.classList.remove("hidden", "low", "critical");
    if (percent <= 10) kegBattery.classList.add("critical");
    else if (percent <= 25) kegBattery.classList.add("low");
    kegBattery.querySelector(".keg-battery-fill").style.width = `${percent}%`;
    kegBattery.querySelector(".keg-battery-value").textContent = `${rounded}%`;
    kegBattery.setAttribute("aria-label", `Keg ${rounded}% remaining by volume`);
  } else {
    kegBattery.className = "keg-battery hidden";
    kegBattery.setAttribute("aria-label", "Keg level unavailable");
  }
  const loginRequired = lanLoginRequired();
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
  if (session && session.session_id !== state.cancelledSessionId
    && ["arming", "armed", "pouring", "settling", "finalizing"].includes(session.status)) {
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

function updateFlowRate(previous, next) {
  const phase = String(next?.device?.status?.state || "unknown").toLowerCase();
  if (phase !== "pouring") {
    state.flowRateMlMin = 0;
    state.lastFlowPulseAt = 0;
    return;
  }
  const priorPulses = decimal(previous?.device?.status?.pulses);
  const nextPulses = decimal(next?.device?.status?.pulses);
  const priorVolume = Number(previous?.live_volume_ml);
  const nextVolume = Number(next?.live_volume_ml);
  const priorTime = Date.parse(previous?.generated_at || "");
  const nextTime = Date.parse(next?.generated_at || "");
  const elapsedMinutes = (nextTime - priorTime) / 60000;
  if (
    nextPulses > priorPulses
    && Number.isFinite(priorVolume)
    && Number.isFinite(nextVolume)
    && nextVolume >= priorVolume
    && elapsedMinutes > 0
  ) {
    const measured = (nextVolume - priorVolume) / elapsedMinutes;
    state.flowRateMlMin = state.flowRateMlMin > 0
      ? state.flowRateMlMin * 0.55 + measured * 0.45
      : measured;
    state.lastFlowPulseAt = performance.now();
  } else if (state.lastFlowPulseAt && performance.now() - state.lastFlowPulseAt > 1200) {
    state.flowRateMlMin = 0;
  }
}

function applySnapshot(snapshot) {
  maybeReloadForNewBuild(snapshot);
  queueMicrotask(() => { void autoArmCamera(); });
  if (state.snapshot && Number(snapshot.revision) < Number(state.snapshot.revision)) return;
  const previousGuideTitle = route() === "/settings" && state.snapshot?.mode === "demo"
    ? demoGuideContext("/settings").guide?.title
    : null;
  const previous = state.snapshot;
  updateFlowRate(previous, snapshot);
  state.snapshot = snapshot;
  syncPourCamera(snapshot);
  syncPourVideo(snapshot);
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
      if (lanLoginRequired(context)) {
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
      if (lanLoginRequired(context)) {
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
  if (lanLoginRequired()) return;
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
    let snapshot;
    try {
      snapshot = JSON.parse(event.data);
    } catch {
      socket.close(1003, "invalid snapshot");
      return;
    }
    if (state.hostAvailable === false || !state.security) {
      applySnapshot(snapshot);
      void refresh();
    } else {
      setHostAvailability(true);
      applySnapshot(snapshot);
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

function page(title, subtitle, content, className = "") {
  return `<section class="${escapeHtml(className)}"><h1 tabindex="-1">${escapeHtml(title)}</h1>${subtitle ? `<p class="lead">${escapeHtml(subtitle)}</p>` : ""}${content}</section>`;
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
  // A calibration/verification capture awaiting its scale mass must not block
  // pours: its pulses are already durably captured, so the tap stays usable.
  return state.hostAvailable !== false
    && state.snapshot?.connection?.state === "connected"
    && currentDevicePhase() === "idle"
    && !state.snapshot?.session;
}

const BEER_ML = 355;
const beersLeft = (ml) => {
  const count = Math.floor(decimal(ml) / BEER_ML);
  return count > 0 ? count : 0;
};

function participantAvatarMarkup(person) {
  if (person?.avatar_updated_at) {
    const src = "/api/v1/participants/" + encodeURIComponent(person.id) + "/avatar?v=" + encodeURIComponent(person.avatar_updated_at);
    return '<img class="avatar-image" src="' + src + '" alt="">';
  }
  const name = String(person?.display_name || "").trim();
  return escapeHtml(name.charAt(0).toUpperCase() || "?");
}

async function shrinkToAvatarJpeg(draw, sourceWidth, sourceHeight, sx, sy, size) {
  const canvas = document.createElement("canvas");
  canvas.width = 128;
  canvas.height = 128;
  canvas.getContext("2d").drawImage(draw, sx, sy, size, size, 0, 0, 128, 128);
  let quality = 0.85;
  let blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
  while (blob && blob.size > 60_000 && quality > 0.3) {
    quality -= 0.1;
    blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
  }
  if (!blob || blob.size > 61_440) return null;
  return blob;
}

async function captureAvatarIfMissing(participantId) {
  try {
    if (!participantId || !state.cameraStream) return;
    const person = (state.snapshot?.participants || []).find((item) => item.id === participantId);
    if (!person || person.avatar_updated_at) return;
    const video = document.createElement("video");
    video.srcObject = state.cameraStream;
    video.muted = true;
    video.playsInline = true;
    await video.play();
    const width = video.videoWidth || 640;
    const height = video.videoHeight || 480;
    let sx = null;
    let sy = null;
    let size = null;
    if ("FaceDetector" in window) {
      try {
        const faces = await new window.FaceDetector({ maxDetectedFaces: 1, fastMode: true }).detect(video);
        if (faces.length) {
          const box = faces[0].boundingBox;
          size = Math.min(Math.max(box.width, box.height) * 1.7, width, height);
          sx = Math.min(Math.max(box.x + box.width / 2 - size / 2, 0), width - size);
          sy = Math.min(Math.max(box.y + box.height / 2 - size / 2, 0), height - size);
        }
      } catch { /* face detection unsupported; use center crop */ }
    }
    if (size === null) {
      size = Math.min(width, height);
      sx = (width - size) / 2;
      sy = (height - size) / 2;
    }
    const blob = await shrinkToAvatarJpeg(video, width, height, sx, sy, size);
    video.srcObject = null;
    if (!blob) return;
    await api("/api/v1/participants/" + encodeURIComponent(participantId) + "/avatar", { method: "POST", headers: { "Content-Type": "image/jpeg" }, body: blob });
  } catch { /* avatar capture is best-effort; arming continues regardless */ }
}

async function jpegFromImageFile(file) {
  const bitmap = await createImageBitmap(file);
  const size = Math.min(bitmap.width, bitmap.height);
  const blob = await shrinkToAvatarJpeg(bitmap, bitmap.width, bitmap.height, (bitmap.width - size) / 2, (bitmap.height - size) / 2, size);
  if (!blob) throw new Error("That image cannot be shrunk into an avatar; try a smaller photo.");
  return blob;
}

function homeView() {
  const s = state.snapshot;
  const onboarding = s.onboarding;
  const inventory = s.inventory;
  const percent = inventory ? Math.max(0, Math.min(100, decimal(inventory.percent_remaining))) : 0;
  const warnings = [];
  if (onboarding.needs_keg) warnings.push(`<li><a href="#/keg">Install the current keg</a> so pours can be assigned to its history.</li>`);
  if (onboarding.needs_calibration) warnings.push(`<li><a href="#/calibration">Complete a ten-pour scale calibration</a>. Pulses captured first are preserved, but their volume and inventory effect remain unknown.</li>`);
  if (s.pending_capture) warnings.push(`<li><a href="#/pour">Enter the scale mass for the waiting ${escapeHtml(s.pending_capture.purpose || "calibration")} sample</a>. Pours stay available meanwhile.</li>`);
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
  const remoteViewer = Boolean(state.security?.lan_mode)
    && !state.security?.local_client
    && !state.security?.authenticated;
  const armDisabled = state.pending.has("arm") || !readyToArm || remoteViewer;
  const calibrated = s.live_volume_ml !== null || Boolean(s.active_calibration);
  const rate = formatFlowRate(state.flowRateMlMin);
  const activePulses = decimal(s.device?.status?.pulses);
  const flowClass = devicePhase === "pouring"
    ? "flowing"
    : devicePhase === "settling" ? "settling" : "idle";
  const connectionClass = s.connection?.state === "connected" ? "connected" : "disconnected";
  const liveAmount = s.live_volume_ml !== null && s.live_volume_ml !== undefined
    ? pourMeasurementText(s.live_volume_ml, s.active_calibration?.default_density_g_per_ml)
    : `${activePulses} raw ${activePulses === 1 ? "pulse" : "pulses"}`;
  const unattributedList = s.unattributed_pours || [];
  const unattributedStrip = unattributedList.length ? `
    <section class="card unattributed-strip" aria-labelledby="unattributed-title">
      <p class="section-kicker">Needs a name</p><h2 id="unattributed-title">Unattributed pours</h2>
      <div class="unattributed-row">${unattributedList.map((row) => `
        <article class="unattributed-card">
          ${row.photo_id
            ? `<img class="unattributed-photo" src="/api/v1/evidence/photos/${encodeURIComponent(row.photo_id)}" alt="Snapshot from this pour" loading="lazy">`
            : '<div class="unattributed-photo placeholder" aria-hidden="true">CAM</div>'}
          <div class="unattributed-copy"><strong>${row.volume_ml !== null && row.volume_ml !== undefined ? escapeHtml(pourMeasurementText(row.volume_ml, row.calibration_density_g_per_ml)) : `${escapeHtml(row.raw_pulses)} pulses`}</strong><p class="muted">${formatTime(row.ended_at)}</p></div>
          ${state.reassignPourId === row.id ? reassignmentEditor(row) : `<button class="secondary" data-action="show-reassign" data-pour="${escapeHtml(row.id)}">Assign</button>`}
        </article>`).join("")}</div>
    </section>` : "";
  const buttons = participants.map((participant) => `
    <button class="participant-button" data-action="arm" data-participant="${escapeHtml(participant.id)}" aria-label="${escapeHtml(participant.display_name)}" aria-describedby="home-device-detail" ${armDisabled ? "disabled" : ""}>
      <span class="participant-avatar" aria-hidden="true">${participantAvatarMarkup(participant)}</span>
      <span>${escapeHtml(participant.display_name)}</span>
      <span class="participant-balance ${Number(participant.balance_cents || 0) < 0 ? "negative" : ""}">${formatMoney(participant.balance_cents || 0)}</span>
    </button>`).join("");
  return page(title, subtitle, `
    <section class="flow-dock ${flowClass} ${connectionClass}" aria-label="Live tap status">
      <div class="flow-dock-copy">
        <div class="flow-heading-row">
          <p class="flow-eyebrow"><span class="flow-status-dot" aria-hidden="true"></span> Live tap</p>
          <span class="flow-phase">${escapeHtml(phaseLabel)}</span>
        </div>
        <div class="flow-metrics">
          <div class="flow-rate" aria-label="Current flow rate"><strong>${calibrated ? escapeHtml(rate.value) : "--"}</strong><span>${escapeHtml(rate.unit)}</span></div>
          <div class="flow-total"><span>Current pour</span><strong>${escapeHtml(liveAmount)}</strong></div>
        </div>
        <p id="home-device-detail" class="flow-detail">${escapeHtml(deviceDetail)}${calibrated ? " Live rate is calibrated." : " Complete calibration to calculate ounces per minute."}</p>
      </div>
      <div class="beer-flow-scene" aria-hidden="true">
        <div class="tap-neck"></div><div class="tap-nozzle"></div>
        <div class="beer-stream"><span></span><span></span></div>
        <div class="beer-glass"><div class="beer-fill"><i></i><i></i><i></i><i></i></div><div class="beer-foam"></div></div>
      </div>
    </section>
    ${warnings.length ? `<aside class="card setup-callout" aria-labelledby="setup-title"><h2 id="setup-title">Setup and review</h2><ul>${warnings.join("")}</ul></aside>` : ""}
    <div class="grid two">
      <section class="card" aria-labelledby="keg-title">
        <h2 id="keg-title">${escapeHtml(s.keg?.label || "No current keg")}</h2>
        <div class="metric">${inventory ? formatVolume(inventory.remaining_ml) : "—"}</div>
        <p>${inventory ? `${percent.toFixed(1)}% remaining · ≈${beersLeft(inventory.remaining_ml)} beers left` : "Configure a keg in Admin."}</p>
        <progress class="progress" max="100" value="${percent}" aria-label="Keg percent remaining">${percent}%</progress>
      </section>
      <section class="card" aria-labelledby="last-title">
        <h2 id="last-title">Last pour</h2>
        ${s.last_pour ? `${pourMeasurementMarkup(s.last_pour.volume_ml, s.last_pour.calibration_density_g_per_ml)}<p>${escapeHtml(s.last_pour.participant_name || "Guest / Unattributed")} · ${formatTime(s.last_pour.ended_at)}</p><p class="muted">${escapeHtml(s.last_pour.quality.replaceAll("_", " "))} · ${escapeHtml(s.last_pour.raw_pulses)} raw pulses</p>` : '<p class="empty">No pours recorded yet.</p>'}
      </section>
    </div>
    <section class="card people-panel" aria-labelledby="people-title">
      <p class="section-kicker">Pour attribution</p><h2 id="people-title">Who is pouring?</h2>
      <div class="participant-grid">
        ${buttons}
        <button class="participant-button guest" data-action="arm" data-participant="" aria-describedby="home-device-detail" ${armDisabled ? "disabled" : ""}><span class="participant-avatar" aria-hidden="true">G</span><span>${participants.length === 0 ? "Start pour" : "Guest / Unattributed"}</span></button>
      </div>
      ${participants.length === 0 ? '<p class="muted">No profiles yet. “Start pour” records an unattributed event; you can assign it later.</p>' : ""}
    </section>
    ${unattributedStrip}
    <p class="kiosk-refresh-row"><button type="button" class="nav-refresh" data-action="reload-page" aria-label="Refresh the screen">↻ Refresh screen</button></p>
  `, "home-page");
}

const formatMoney = (cents) => new Intl.NumberFormat(undefined, {
  style: "currency", currency: "USD",
}).format(Number(cents || 0) / 100);

async function loadManagement() {
  if (!state.security?.pin_configured || !state.security?.authenticated) {
    state.management = null;
    render();
    return;
  }
  try {
    state.management = await api("/api/v1/management");
    render();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      state.security = await refreshSecurityContext();
      state.management = null;
      render();
      return;
    }
    showError(error);
  }
}

function managementView() {
  if (!state.security?.pin_configured) {
    return page("Management", "Pricing, balances, and pour evidence require an administrator PIN.", `
      <section class="card narrow-card"><h2>Create administrator PIN</h2>
      <form id="pin-form" class="stack" data-pin-form><span class="field-label">New PIN</span><button type="button" class="pin-display" data-action="open-keypad" aria-label="New PIN">Tap to enter PIN</button><input type="hidden" name="pin" autocomplete="off"><button>Protect management</button></form></section>`);
  }
  if (!state.security?.authenticated) {
    return page("Management", "Unlock the protected controls with the administrator PIN.", `<section class="card narrow-card">${loginFormMarkup(true)}</section>`);
  }
  const m = state.management;
  if (!m) return page("Management", "Loading protected account and camera settings.", '<section class="card"><button data-action="load-management">Load management</button></section>');
  const people = m.participants.map((person) => `<article class="account-row">
    <div class="account-identity"><span class="participant-avatar" aria-hidden="true">${participantAvatarMarkup(person)}</span><div><h3>${escapeHtml(person.display_name)}</h3><p class="account-balance">${formatMoney(person.balance_cents)}</p></div></div>
    <div class="avatar-actions"><label class="button secondary avatar-upload">Change photo<input type="file" accept="image/*" class="avatar-input" data-participant="${escapeHtml(person.id)}" hidden></label>${person.avatar_updated_at ? `<button type="button" class="secondary" data-action="avatar-remove" data-participant="${escapeHtml(person.id)}">Remove photo</button>` : ""}</div>
    <form class="fund-form stack" data-participant="${escapeHtml(person.id)}">
      <label>Funds change ($)<input name="amount_dollars" type="number" step="0.01" min="-100000" max="100000" placeholder="25.00" required></label>
      <label>Reason<input name="reason" maxlength="500" placeholder="Cash added" required></label>
      <button>Record funds</button>
    </form></article>`).join("") || '<p class="empty">Add people before managing balances.</p>';
  const ledger = m.ledger.map((entry) => `<tr><td>${escapeHtml(new Date(entry.created_at).toLocaleString())}</td><td>${escapeHtml(entry.participant_name)}</td><td>${escapeHtml(entry.kind)}</td><td class="money ${Number(entry.amount_cents) < 0 ? "negative" : "positive"}">${formatMoney(entry.amount_cents)}</td><td>${escapeHtml(entry.reason)}</td></tr>`).join("") || '<tr><td colspan="5">No account activity yet.</td></tr>';
  const photos = m.photos.map((photo) => `<figure class="evidence-photo"><img src="/api/v1/management/photos/${encodeURIComponent(photo.id)}" alt="Pour evidence captured ${escapeHtml(new Date(photo.captured_at).toLocaleString())}" loading="lazy"><figcaption>${escapeHtml(photo.participant_name || "Unattributed")}<br>${escapeHtml(new Date(photo.captured_at).toLocaleString())}</figcaption></figure>`).join("") || '<p class="empty">No pour photos have been captured.</p>';
  const keg = state.snapshot?.keg;
  const inventory = state.snapshot?.inventory;
  const kegControl = keg && inventory
    ? `<section class="card"><h2>Keg level</h2><p class="metric">${escapeHtml(Number(inventory.percent_remaining).toFixed(1))}%</p><p>${escapeHtml(formatVolume(inventory.remaining_ml))} remaining in ${escapeHtml(keg.label)}.</p><form id="keg-remaining-form" class="stack"><label>Set remaining volume (%)<input name="percent_remaining" type="number" min="0" max="100" step="0.1" value="${escapeHtml(Number(inventory.percent_remaining).toFixed(1))}" required></label><label>Reason<input name="reason" maxlength="500" value="Manual keg level correction" required></label><button>Update keg level</button></form><p class="field-help">This records an audited inventory correction; measured pour history is not changed.</p></section>`
    : '<section class="card"><h2>Keg level</h2><p class="empty">Install a keg before setting its remaining level.</p></section>';
  return page("Management", "Protected pricing, participant funds, and local pour evidence.", `
    <div class="grid two management-grid"><section class="card"><h2>Beer price</h2><form id="management-settings-form" class="stack"><label>Price per US fl oz ($)<input name="price_per_fl_oz" type="number" min="0" max="1000" step="0.01" value="${escapeHtml((Number(m.price_cents_per_fl_oz) / 100).toFixed(2))}" required></label><button>Save price</button></form><p class="field-help">Each completed attributed pour stores the price used at that moment.</p></section>
    ${kegControl}<section class="card"><h2>Pour camera</h2><div class="camera-preview"><video id="camera-preview" autoplay muted playsinline></video><span>${escapeHtml(state.cameraStatus)}</span></div><p>Photos stay on this computer and are taken once per second only while beer is flowing.</p><div class="button-row"><button data-action="camera-enable">${state.cameraStream ? "Camera armed" : "Enable camera"}</button><button class="secondary" data-action="camera-disable" ${!m.webcam_enabled && !state.cameraStream ? "disabled" : ""}>Disable</button></div><p class="field-help">Check framing without pouring: this records a five-second clip into your KegPulse videos folder.</p><div class="button-row"><button class="secondary" data-action="camera-test" ${state.cameraStream ? "" : "disabled"}>${state.cameraTesting ? "Recording test clip\u2026" : "Record 5-second test clip"}</button></div></section></div>
    <section class="management-band"><h2>Participant funds</h2><div class="account-list">${people}</div></section>
    <section class="management-band"><h2>Account ledger</h2><div class="table-wrap"><table><thead><tr><th>Time</th><th>Person</th><th>Type</th><th>Amount</th><th>Reason</th></tr></thead><tbody>${ledger}</tbody></table></div></section>
    <section class="management-band"><h2>Pour evidence</h2><div class="evidence-grid">${photos}</div></section>`);
}

function attachCameraPreview() {
  for (const video of document.querySelectorAll(".camera-preview video")) {
    if (state.cameraStream) video.srcObject = state.cameraStream;
  }
}

function cameraShouldRun() {
  const fromSnapshot = state.snapshot?.settings?.webcam_enabled;
  if (fromSnapshot !== undefined && fromSnapshot !== null) return Boolean(fromSnapshot);
  return Boolean(state.management?.webcam_enabled);
}

async function armCameraStream() {
  if (!navigator.mediaDevices?.getUserMedia) throw new Error("This browser does not provide camera access.");
  const stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 640 }, height: { ideal: 480 } }, audio: false });
  state.cameraStream?.getTracks().forEach((track) => track.stop());
  state.cameraStream = stream;
  state.cameraStatus = "Camera armed for the next pour";
  document.body.dataset.camera = "armed";
  stream.getVideoTracks()[0]?.addEventListener("ended", () => {
    if (state.cameraStream === stream) {
      state.cameraStream = null;
      state.cameraStatus = "Camera disconnected";
      delete document.body.dataset.camera;
      state.cameraAutoArmAt = 0; // allow a fresh auto-arm attempt
    }
  });
}

// Kiosks (re)open the camera on every load when it is enabled, so a refresh
// or reboot never silently loses pour evidence. Retries with backoff while the
// device is missing or permission is pending.
async function autoArmCamera() {
  if (state.cameraStream || !cameraShouldRun() || state.cameraAutoArming) return;
  if (state.security?.lan_mode && state.security?.local_client === false) return; // remote viewers never own the camera
  const now = performance.now();
  if (now - (state.cameraAutoArmAt || 0) < 15_000) return;
  state.cameraAutoArmAt = now;
  state.cameraAutoArming = true;
  try {
    await armCameraStream();
    render();
  } catch (error) {
    state.cameraStatus = `Camera unavailable: ${error instanceof Error ? error.message : String(error)}`;
  } finally {
    state.cameraAutoArming = false;
  }
}

async function enableCamera() {
  if (state.pending.has("camera-enable")) return;
  try {
    await armCameraStream();
    const enabled = await mutation("camera-enable", "/api/v1/management/settings", { webcam_enabled: true }, "PATCH");
    if (enabled) state.management = enabled;
    render();
  } catch (error) {
    state.cameraStream?.getTracks().forEach((track) => track.stop());
    state.cameraStream = null;
    state.cameraStatus = "Camera permission unavailable";
    showError(error);
    render();
  }
}

async function disableCamera() {
  state.cameraStream?.getTracks().forEach((track) => track.stop());
  state.cameraStream = null;
  delete document.body.dataset.camera;
  state.cameraStatus = "Camera disabled";
  try {
    const disabled = await mutation("camera-disable", "/api/v1/management/settings", { webcam_enabled: false }, "PATCH");
    if (disabled) state.management = disabled;
    render();
  } catch (error) { showError(error); }
}

async function recordCameraTestClip() {
  if (state.cameraTesting) return;
  if (!state.cameraStream) { showError("Enable the camera first."); return; }
  if (typeof MediaRecorder === "undefined") { showError("This browser cannot record video."); return; }
  state.cameraTesting = true;
  render();
  try {
    const mime = MediaRecorder.isTypeSupported?.("video/webm;codecs=vp8") ? "video/webm;codecs=vp8" : "video/webm";
    const recorder = new MediaRecorder(state.cameraStream, { mimeType: mime, videoBitsPerSecond: 1_200_000 });
    const chunks = [];
    const finished = new Promise((resolve) => { recorder.onstop = resolve; });
    recorder.ondataavailable = (event) => { if (event.data?.size) chunks.push(event.data); };
    recorder.start(500);
    await new Promise((resolve) => setTimeout(resolve, 5000));
    try { recorder.requestData(); } catch { /* not every browser exposes this */ }
    recorder.stop();
    await finished;
    const blob = new Blob(chunks, { type: "video/webm" });
    if (!blob.size) throw new Error("The camera produced no video data.");
    const stored = await api("/api/v1/evidence/test-video", { method: "POST", headers: { "Content-Type": "video/webm" }, body: blob });
    showToast(`Test clip saved: ${stored.file}`);
  } catch (error) {
    showError(error);
  } finally {
    state.cameraTesting = false;
    render();
  }
}

async function capturePourPhoto(sessionId) {
  if (state.cameraUploadActive || !state.cameraStream) return;
  const track = state.cameraStream.getVideoTracks()[0];
  const settings = track?.getSettings?.() || {};
  const video = document.createElement("video");
  video.srcObject = state.cameraStream;
  video.muted = true;
  video.playsInline = true;
  await video.play();
  const sourceWidth = video.videoWidth || settings.width || 640;
  const sourceHeight = video.videoHeight || settings.height || 480;
  const width = Math.min(480, sourceWidth);
  const height = Math.max(1, Math.round(sourceHeight * width / sourceWidth));
  const canvas = document.createElement("canvas");
  canvas.width = width; canvas.height = height;
  canvas.getContext("2d").drawImage(video, 0, 0, width, height);
  video.srcObject = null;
  let quality = 0.62;
  let blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
  while (blob && blob.size > 60_000 && quality > 0.25) {
    quality -= 0.1;
    blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
  }
  if (!blob || blob.size > 61_440) throw new Error("Camera frame is too large to store safely.");
  state.cameraUploadActive = true;
  try {
    const target = sessionId
      ? `/api/v1/sessions/${encodeURIComponent(sessionId)}/photos`
      : "/api/v1/evidence/photos";
    await api(target, { method: "POST", headers: { "Content-Type": "image/jpeg" }, body: blob });
    state.cameraStatus = sessionId ? "Capturing this pour" : "Capturing unattributed flow";
  } finally {
    state.cameraUploadActive = false;
  }
}

function syncPourVideo(snapshot) {
  const phase = String(snapshot?.device?.status?.state || "").toLowerCase();
  const session = snapshot?.session;
  const activePour = (session?.purpose === "pour" && ["armed", "pouring", "settling"].includes(phase))
    || (!session && ["pouring", "settling"].includes(phase));
  if (["pouring", "settling"].includes(phase)) state.videoSawFlow = true;
  if (
    activePour
    && state.cameraStream
    && cameraShouldRun()
    && !state.videoRecorder
    && typeof MediaRecorder !== "undefined"
  ) {
    try {
      const mime = MediaRecorder.isTypeSupported?.("video/webm;codecs=vp8") ? "video/webm;codecs=vp8" : "video/webm";
      const recorder = new MediaRecorder(state.cameraStream, { mimeType: mime, videoBitsPerSecond: 1_200_000 });
      const chunks = [];
      const recordingSession = session ? session.session_id : "unattributed";
      state.videoSessionId = recordingSession;
      state.videoSawFlow = ["pouring", "settling"].includes(phase);
      recorder.ondataavailable = (event) => { if (event.data?.size) chunks.push(event.data); };
      const sawFlowRef = () => state.videoSawFlow;
      recorder.onstop = () => {
        const keep = sawFlowRef();
        if (state.videoRecorder === recorder) {
          state.videoRecorder = null;
          state.videoSessionId = null;
          state.videoSawFlow = false;
        }
        // An arm that timed out without flow produces no evidence worth keeping.
        if (keep) void uploadPourVideo(recordingSession, chunks);
      };
      recorder.start(500);
      state.videoRecorder = recorder;
      setTimeout(() => {
        if (state.videoRecorder === recorder && recorder.state !== "inactive") recorder.stop();
      }, 120_000);
    } catch { /* video recording unsupported on this browser */ }
  } else if (state.videoRecorder && (!activePour || (session ? session.session_id : "unattributed") !== state.videoSessionId)) {
    const recorder = state.videoRecorder;
    if (recorder.state !== "inactive") {
      // requestData flushes the in-progress chunk so very short pours are not
      // finalized as an empty clip.
      try { recorder.requestData(); } catch { /* not all browsers expose this */ }
      recorder.stop();
    } else {
      state.videoRecorder = null;
      state.videoSessionId = null;
      state.videoSawFlow = false;
    }
  }
}

async function uploadPourVideo(sessionId, chunks) {
  if (!sessionId || !chunks.length) return;
  const blob = new Blob(chunks, { type: "video/webm" });
  if (blob.size < 4 || blob.size > 33_554_432) return;
  const target = sessionId === "unattributed"
    ? "/api/v1/evidence/videos"
    : "/api/v1/sessions/" + encodeURIComponent(sessionId) + "/videos";
  try {
    await api(target, { method: "POST", headers: { "Content-Type": "video/webm" }, body: blob });
  } catch { /* keep the pour flow quiet if the video cannot be stored */ }
}

function syncPourCamera(snapshot) {
  const phase = String(snapshot?.device?.status?.state || "").toLowerCase();
  const session = snapshot?.session;
  const attributedPour = session?.purpose === "pour";
  const unattributedFlow = !session;
  if (!state.cameraStream || !cameraShouldRun() || phase !== "pouring" || (!attributedPour && !unattributedFlow)) return;
  const now = performance.now();
  if (now - state.cameraLastCapture < 1000) return;
  state.cameraLastCapture = now;
  void capturePourPhoto(attributedPour ? session.session_id : null).catch((error) => {
    state.cameraStatus = "Camera capture failed";
    showError(error);
  });
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
  const cameraEnabled = cameraShouldRun();
  const cameraReady = cameraEnabled && Boolean(state.cameraStream);
  const cameraCapturing = cameraReady && status === "pouring";
  const cameraLabel = cameraCapturing
    ? "Recording pour evidence"
    : cameraReady
      ? status === "settling" ? "Camera paused while flow is stopped" : "Camera ready; recording starts with flow"
      : cameraEnabled ? "Camera enabled but unavailable in this browser" : "Camera recording is off";
  const cameraMonitor = purpose === "pour" ? `<aside class="pour-camera-monitor ${cameraCapturing ? "recording" : cameraReady ? "ready" : "off"}" aria-label="Pour camera status">
    ${cameraReady ? '<div class="camera-preview pour-camera-preview"><video autoplay muted playsinline></video></div>' : '<div class="camera-placeholder" aria-hidden="true">CAM</div>'}
    <div><p class="camera-state"><span class="camera-record-dot" aria-hidden="true"></span>${escapeHtml(cameraLabel)}</p><p class="camera-disclosure">Local snapshots are saved once per second only while beer is flowing.</p></div>
  </aside>` : "";
  return `<section class="pour-screen"><div class="pour-panel card">
    <p class="pour-state">${escapeHtml(status)}</p>
    <h1 tabindex="-1">${escapeHtml(title)}</h1>
    ${["timed_out", "interrupted_uncertain"].includes(status) ? `<div class="pour-amount">${status === "timed_out" ? "No flow" : "Needs review"}</div>` : pourMeasurementMarkup(volume, s.active_calibration?.default_density_g_per_ml, "output")}
    <p class="countdown" aria-live="polite">${escapeHtml(countdown)}</p>
    <p>${escapeHtml(note)}</p>
    ${cameraMonitor}
    ${["timed_out", "interrupted_uncertain"].includes(status) ? '<button data-action="dismiss-terminal">Return home</button>' : status === "complete" && purpose !== "pour" ? '<div class="button-row"><a class="button" href="#/calibration">Enter scale mass</a><button class="secondary" data-action="discard-capture">Discard sample</button></div>' : `
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
      ${pourMeasurementMarkup(pour.volume_ml, pour.calibration_density_g_per_ml)}
      <p>${escapeHtml(pour.participant_name || "Guest / Unattributed")} · ${escapeHtml(pour.raw_pulses)} raw pulses</p>
      ${warning ? `<p class="warning-text">Review needed: ${escapeHtml(pour.quality.replaceAll("_", " "))}. Counted pulses were retained.</p>` : '<p class="good-text">Complete measurement saved.</p>'}
      <p id="return-countdown" class="muted">${state.completionPaused ? "Auto-return paused." : "Returning home shortly. Interaction pauses auto-return."}</p>
      <div class="button-row"><button data-action="home-now">Return home</button><button class="secondary" data-action="stay">Stay here</button></div>
    </section>`);
}

function scheduleCompletionReturn() {
  if (state.completionPaused || state.completionTimer) return;
  const seconds = Number(state.snapshot?.settings?.completion_seconds ?? 9);
  if (seconds > 0) {
    state.completionTimer = window.setTimeout(() => {
      state.completionTimer = null;
      navigate("/");
    }, seconds * 1000);
  }
}

function pauseCompletionReturn() {
  state.completionPaused = true;
  window.clearTimeout(state.completionTimer);
  state.completionTimer = null;
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
    <tr><td>${formatTime(row.ended_at)}</td><td>${escapeHtml(row.participant_name || "Guest / Unattributed")}</td><td>${escapeHtml(pourMeasurementText(row.volume_ml, row.calibration_density_g_per_ml))}</td><td>${escapeHtml(row.raw_pulses)} pulses<br>${escapeHtml(row.quality.replaceAll("_", " "))}${pourDetails(row)}</td><td>${action(row)}</td></tr>`).join("")}</tbody></table></div>
    <div class="sample-cards">${rows.map((row) => `<article class="sample-card"><strong>${escapeHtml(pourMeasurementText(row.volume_ml, row.calibration_density_g_per_ml))}</strong><br>${escapeHtml(row.participant_name || "Guest / Unattributed")}<br><span class="muted">${formatTime(row.ended_at)} · ${escapeHtml(row.raw_pulses)} pulses · ${escapeHtml(row.quality.replaceAll("_", " "))}</span>${pourDetails(row)}${action(row)}</article>`).join("")}</div>`;
}

function pourDetails(row) {
  return `<details class="pour-details" data-pour-details="${escapeHtml(row.id)}" ${state.openPourDetails?.has(row.id) ? "open" : ""}><summary>Measurement details</summary><dl class="status-list">
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
  if (state.security?.pin_configured && !state.security?.authenticated) {
    return page("Keg inventory locked", "Installing, replacing, or correcting a keg rewrites inventory, so it requires the administrator PIN.", `<section class="card narrow-card">${loginFormMarkup(true)}</section>`);
  }
  const keg = state.snapshot.keg;
  const inventory = state.snapshot.inventory;
  return page("Keg inventory", "Replacing a keg closes its history. Manual corrections always require a reason.", `
    <div class="grid two">
      <section class="card"><h2>Current keg</h2>${keg ? `
        <dl class="status-list"><dt>Label</dt><dd>${escapeHtml(keg.label)}</dd><dt>Installed</dt><dd>${formatTime(keg.opened_at)}</dd><dt>Starting</dt><dd>${formatVolume(keg.starting_volume_ml)}</dd><dt>Remaining</dt><dd>${formatVolume(inventory?.remaining_ml)}</dd><dt>Beers left</dt><dd>≈${beersLeft(inventory?.remaining_ml)} (12 oz)</dd><dt>Poured</dt><dd>${formatVolume(inventory?.poured_ml)}</dd><dt>Adjustments</dt><dd>${formatVolume(inventory?.adjustments_ml, "ml")}</dd></dl>
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
  const includedCount = analysis?.included_count ?? detail.samples.filter((x) => x.included).length;
  const canJudgeConsistency = includedCount >= 3;
  const consistencyLabel = (sample, flagged) => !sample.included
    ? flagged ? "Excluded — suspected outlier" : "Excluded by user"
    : flagged ? "Suspected outlier"
      : canJudgeConsistency ? "Consistent" : "Too few samples to judge";
  const rows = detail.samples.map((sample, index) => {
    const a = analysis?.samples?.[index];
    const flagged = a?.suspected_outlier || sample.suspected_outlier;
    const consistency = consistencyLabel(sample, flagged);
    const density = Number(detail.default_density_g_per_ml) || 1;
    const predicted = a ? `${Number(a.predicted_volume_ml).toFixed(2)} mL · ≈${(Number(a.predicted_volume_ml) * density).toFixed(1)} g` : "—";
    const residual = a ? `${Number(a.residual_ml).toFixed(2)} mL (${Number(a.percentage_error).toFixed(2)}%)` : "—";
    const action = editable
      ? `<td><button class="secondary" data-action="toggle-sample" data-calibration="${escapeHtml(detail.id)}" data-ordinal="${sample.ordinal}" data-included="${sample.included ? "0" : "1"}">${sample.included ? "Exclude" : "Include"}</button></td>`
      : "";
    return `<tr class="${flagged ? "outlier" : !sample.included ? "excluded" : ""}"><td>${sample.ordinal}</td><td>${sample.raw_pulses}</td><td>${sample.mass_g} g</td><td>${Number(sample.derived_volume_ml).toFixed(2)} mL</td><td>${predicted}</td><td>${residual}</td><td>${consistency}</td>${action}</tr>`;
  }).join("");
  const cards = detail.samples.map((sample, index) => {
    const a = analysis?.samples?.[index];
    const flagged = a?.suspected_outlier || sample.suspected_outlier;
    const consistency = consistencyLabel(sample, flagged);
    const consistencyClass = flagged
      ? "warning-text"
      : sample.included ? (canJudgeConsistency ? "good-text" : "muted") : "muted";
    const action = editable
      ? `<div><button class="secondary" data-action="toggle-sample" data-calibration="${escapeHtml(detail.id)}" data-ordinal="${sample.ordinal}" data-included="${sample.included ? "0" : "1"}">${sample.included ? "Exclude" : "Include"}</button></div>`
      : "";
    return `<article class="sample-card ${flagged ? "outlier" : !sample.included ? "excluded" : ""}"><strong>Sample ${sample.ordinal}</strong><p>${sample.raw_pulses} pulses · ${sample.mass_g} g</p><dl class="sample-metrics"><dt>Actual scale volume</dt><dd>${Number(sample.derived_volume_ml).toFixed(2)} mL</dd><dt>Predicted volume</dt><dd>${a ? `${Number(a.predicted_volume_ml).toFixed(2)} mL` : "—"}</dd><dt>Predicted weight</dt><dd>${a ? `≈${(Number(a.predicted_volume_ml) * (Number(detail.default_density_g_per_ml) || 1)).toFixed(1)} g` : "—"}</dd><dt>Residual / error</dt><dd>${a ? `${Number(a.residual_ml).toFixed(2)} mL (${Number(a.percentage_error).toFixed(2)}%)` : "—"}</dd></dl><p class="${consistencyClass}">${consistency}</p>${action}</article>`;
  }).join("");
  const guidance = editable
    ? "Suspected outliers remain included until you decide."
    : `This ${escapeHtml(detail.status)} calibration is read-only; its inclusion decisions are preserved.`;
  const fullyActivatable = detail.samples.length === 10 && includedCount >= 7;
  const analysisSummary = analysis
    ? `<p><strong>Aggregate factor:</strong> ${Number(analysis.pulses_per_ml).toFixed(6)} pulses/mL · variation ${analysis.coefficient_of_variation_pct === null || analysis.coefficient_of_variation_pct === undefined ? "n/a (need 2+ samples)" : `${Number(analysis.coefficient_of_variation_pct).toFixed(2)}%`}</p>${editable && fullyActivatable ? `<button data-action="activate-calibration" data-calibration="${escapeHtml(detail.id)}">Review and activate</button>` : editable ? '<p class="muted">Capture all ten samples and keep at least seven included to activate.</p>' : ""}`
    : editable
      ? '<p class="muted">Capture all ten samples and keep at least seven included to activate.</p>'
      : "";
  const provisionalAction = editable
    && detail.samples.length >= 1
    && detail.samples.length < 10
    && includedCount >= 1
    ? `<aside class="banner warning provisional-calibration"><strong>Temporary estimate only.</strong> ${includedCount < 3 ? "So few samples cannot measure repeatability or identify an outlier." : "A partial run cannot fully measure repeatability; review any flagged outlier before relying on it."}<button class="secondary" data-action="activate-provisional-calibration" data-calibration="${escapeHtml(detail.id)}">Use ${includedCount}-sample estimate for now</button></aside>`
    : "";
  return `<section class="card"><h2>Sample review</h2><p>${detail.samples.length}/10 captured · ${analysis?.included_count ?? detail.samples.filter((x) => x.included).length} included. ${guidance}</p>
    <div class="table-wrap"><table><thead><tr><th>#</th><th>Pulses</th><th>Mass</th><th>Actual scale volume</th><th>Predicted volume</th><th>Residual / error</th><th>Consistency</th>${editable ? "<th>Use sample</th>" : ""}</tr></thead><tbody>${rows}</tbody></table></div><div class="sample-cards">${cards}</div>
    ${analysisSummary}${provisionalAction}
  </section>`;
}

function calibrationCaptureCard(capture, captureDensity) {
  if (capture?.status !== "complete") return "";
  return `<section class="card quick-cal"><h2>${capture.purpose === "verification" ? "Enter verification mass" : `Enter mass for sample ${capture.target_ordinal}`}</h2><p>${escapeHtml(capture.captured_raw_pulses)} raw pulses captured. Selected density: <strong>${escapeHtml(captureDensity)} g/mL</strong>.</p><form id="capture-commit-form" data-purpose="${capture.purpose}" data-session="${capture.session_id}" data-calibration="${capture.calibration_id || ""}" class="grid two"><label>Scale mass (g)<input name="mass_g" type="number" inputmode="decimal" min="0.1" max="10000" step="0.01" required></label><label>Density (g/mL)<input name="density_g_per_ml" type="number" inputmode="decimal" min="0.5" max="2" step="0.001" value="${escapeHtml(captureDensity)}" required></label>${capture.purpose === "calibration" ? '<label><input name="included" type="checkbox" checked> Include this sample</label>' : ""}<div class="button-row"><button>Save measured check</button><button type="button" class="secondary" data-action="discard-capture">Discard sample</button></div></form></section>`;
}

function calibrationRunCard(detail, { hero = false } = {}) {
  const editable = detail.status === "draft";
  const captureButton = editable && detail.samples.length < 10
    ? `<button class="${hero ? "capture-cta" : ""}" data-action="capture-sample" data-calibration="${escapeHtml(detail.id)}" data-ordinal="${detail.samples.length + 1}">Capture sample ${detail.samples.length + 1}</button>`
    : "";
  const kicker = hero ? '<p class="section-kicker">Calibration in progress</p>' : "";
  const title = hero
    ? `<h2>${escapeHtml(detail.liquid)} \u00b7 ${escapeHtml(detail.default_density_g_per_ml)} g/mL</h2>`
    : `<h2>${escapeHtml(detail.liquid)} \u00b7 ${escapeHtml(detail.status)}</h2>`;
  return `<article class="card ${hero ? "quick-cal" : ""}" data-calibration-status="${escapeHtml(detail.status)}">${kicker}${title}<p>Created ${formatTime(detail.created_at)}</p>${captureButton}${detail.samples.length ? sampleReview(detail) : '<p class="empty">No samples captured yet \u2014 pour one and weigh it.</p>'}</article>`;
}

function calibrationView() {
  if (state.security?.pin_configured && !state.security?.authenticated) {
    return page("Calibration locked", "Calibration changes the pulse-to-volume factor behind every pour, so it requires the administrator PIN.", `<section class="card narrow-card">${loginFormMarkup(true)}</section>`);
  }
  const active = state.snapshot.active_calibration;
  const capture = state.snapshot.pending_capture;
  const verification = state.snapshot.last_verification;
  const details = state.calibrationDetails;
  const captureDensity = capture?.density_g_per_ml
    || details?.find((item) => item.id === capture?.calibration_id)?.default_density_g_per_ml
    || "1.010";
  const drafts = (details || []).filter((item) => item.status === "draft")
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const heroDraft = drafts[0] || null;
  const pastRuns = (details || []).filter((item) => item !== heroDraft);

  const heroSection = details === null
    ? '<section class="card quick-cal"><p class="muted">Loading calibration runs\u2026</p></section>'
    : heroDraft
      ? calibrationRunCard(heroDraft, { hero: true })
      : `<section class="card quick-cal"><p class="section-kicker">Quick calibration</p><h2>Three pours and you're calibrated</h2><ol class="step-list"><li>Start a run \u2014 preset for beer at 1.010 g/mL.</li><li>Capture a pour, weigh the glass, enter the grams.</li><li>Repeat for three varied pour sizes, then tap \u201cUse 3-sample estimate for now\u201d.</li></ol><form id="calibration-form" class="stack"><div class="grid two"><label>Liquid<input name="liquid" maxlength="80" value="beer" required></label><label>Density (g/mL)<input name="density_g_per_ml" type="number" inputmode="decimal" min="0.5" max="2" step="0.001" value="1.010" required></label></div><span class="field-help">Most lagers and ales are about 1.010 g/mL; stouts run nearer 1.015. Use 1.000 for water.</span><label>Notes<textarea name="notes" maxlength="1000"></textarea></label><button>Start calibration run</button></form><p class="warning-text">Density directly affects volume. KegPulse is not a legal-for-trade meter.</p></section>`;

  const activeSection = `<section class="card"><h2>Active calibration</h2>${active ? `${String(active.notes || "").includes("[PROVISIONAL:") ? '<p class="warning-text">Provisional estimate from a partial sample run. Add samples to the current run and re-activate whenever you want tighter accuracy.</p>' : ""}<dl class="status-list"><dt>Liquid</dt><dd>${escapeHtml(active.liquid)}</dd><dt>Factor</dt><dd>${Number(active.pulses_per_ml).toFixed(6)} pulses/mL</dd><dt>Activated</dt><dd>${formatTime(active.activated_at)}</dd></dl><button data-action="start-verification">Start weighed verification pour</button>` : '<p class="empty">No calibration is active yet \u2014 finish the quick run above.</p>'}</section>`;

  const verificationSection = verification
    ? `<section class="card ${verification.warning ? "outlier" : ""}"><h2>Latest verification</h2><dl class="status-list"><dt>Predicted</dt><dd>${formatVolume(verification.predicted_volume_ml)}</dd><dt>Scale volume</dt><dd>${formatVolume(verification.actual_volume_ml)}</dd><dt>Absolute error</dt><dd>${formatVolume(verification.absolute_error_ml)}</dd><dt>Percentage error</dt><dd>${Number(verification.percentage_error).toFixed(2)}%</dd></dl><p class="${verification.warning ? "warning-text" : "good-text"}">${verification.warning ? "Drift warning: investigate sensor, flow conditions, tubing, or calibration. The factor was not changed." : "Verification is within the configured warning threshold."}</p></section>`
    : "";

  const newRunSection = heroDraft
    ? `<section class="card"><h2>Start another run</h2><form id="calibration-form" class="stack"><div class="grid two"><label>Liquid<input name="liquid" maxlength="80" value="beer" required></label><label>Density (g/mL)<input name="density_g_per_ml" type="number" inputmode="decimal" min="0.5" max="2" step="0.001" value="1.010" required></label></div><label>Notes<textarea name="notes" maxlength="1000"></textarea></label><button>Start calibration run</button></form></section>`
    : "";

  const pastSection = pastRuns.length
    ? `<section class="stack" id="calibration-runs"><h2 class="section-heading">Past runs</h2>${pastRuns.map((item) => calibrationRunCard(item)).join("")}</section>`
    : `<section id="calibration-runs" class="stack">${details ? "" : '<button data-action="load-calibrations" class="secondary">Load calibration runs</button>'}</section>`;

  return page("Calibration", "Pour, weigh, enter grams \u2014 three good pours make a working estimate.", `
    ${calibrationCaptureCard(capture, captureDensity)}
    ${heroSection}
    <div class="grid two">
      ${activeSection}
      ${verificationSection || newRunSection}
    </div>
    ${verificationSection && heroDraft ? newRunSection : ""}
    ${pastSection}
  `);
}

const BOARD_TABS = [
  ["keg", "Keg"],
  ["pours", "Pours"],
  ["people", "People"],
  ["unrecorded", "Unrecorded"],
];
const ML_PER_FL_OZ = 29.5735;
const flOz = (ml) => decimal(ml) / ML_PER_FL_OZ;
const SEPTEMBER_COUNTER_START = Date.parse("2026-08-30T22:16:01Z");

function septemberTopDrinkers(pours, participants) {
  const counts = new Map();
  for (const pour of pours) {
    if (!pour.participant_id || Date.parse(pour.ended_at) < SEPTEMBER_COUNTER_START) continue;
    if (pour.volume_ml === null || pour.volume_ml === undefined || decimal(pour.volume_ml) <= 0) continue;
    counts.set(pour.participant_id, (counts.get(pour.participant_id) || 0) + 1);
  }
  return participants.map((person) => ({
    ...person,
    septemberDrinks: counts.get(person.id) || 0,
  })).sort((a, b) => b.septemberDrinks - a.septemberDrinks
    || a.display_name.localeCompare(b.display_name, undefined, { sensitivity: "base" })
    || a.id.localeCompare(b.id)).slice(0, 5);
}

function boardPoursStale() {
  const lastId = state.snapshot?.last_pour?.id || null;
  return state.boardPours === null
    || lastId !== state.boardPoursForLastPour
    || Date.now() - state.boardPoursAt > 60_000;
}

async function loadBoardPours() {
  try {
    const pours = await api("/api/v1/history?limit=500");
    state.boardPours = Array.isArray(pours) ? pours : [];
    state.boardPoursAt = Date.now();
    state.boardPoursForLastPour = state.snapshot?.last_pour?.id || null;
    if (route() === "/display") render();
  } catch (error) {
    state.boardPoursAt = Date.now(); // back off; the next snapshot retries
    showError(error);
  }
}

function boardDailySeries(pours, days = 14) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const buckets = [];
  for (let offset = days - 1; offset >= 0; offset -= 1) {
    const day = new Date(today);
    day.setDate(today.getDate() - offset);
    buckets.push({ key: day.toDateString(), label: day.toLocaleDateString(undefined, { month: "short", day: "numeric" }), oz: 0, count: 0 });
  }
  const byKey = new Map(buckets.map((bucket) => [bucket.key, bucket]));
  for (const pour of pours) {
    if (pour.volume_ml === null || pour.volume_ml === undefined) continue;
    const bucket = byKey.get(new Date(pour.ended_at).toDateString());
    if (!bucket) continue;
    bucket.oz += flOz(pour.volume_ml);
    bucket.count += 1;
  }
  return buckets;
}

function niceCeiling(value) {
  if (value <= 0) return 10;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (value <= step * magnitude) return step * magnitude;
  }
  return 10 * magnitude;
}

function boardColumnChart(series) {
  const width = 720; const height = 240;
  const pad = { top: 22, right: 12, bottom: 30, left: 44 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const max = niceCeiling(Math.max(...series.map((item) => item.oz), 0));
  const band = plotW / series.length;
  const barW = Math.min(24, Math.max(6, band - 8));
  const yFor = (oz) => pad.top + plotH - (oz / max) * plotH;
  const ticks = [0, max / 2, max];
  const grid = ticks.map((tick) => `<line class="board-grid" x1="${pad.left}" x2="${width - pad.right}" y1="${yFor(tick).toFixed(1)}" y2="${yFor(tick).toFixed(1)}"></line><text class="board-tick" x="${pad.left - 8}" y="${(yFor(tick) + 4).toFixed(1)}" text-anchor="end">${tick % 1 === 0 ? tick : tick.toFixed(1)}</text>`).join("");
  const peak = Math.max(...series.map((item) => item.oz), 0);
  const bars = series.map((item, index) => {
    const x = pad.left + band * index + (band - barW) / 2;
    const y = yFor(item.oz);
    const barH = Math.max(0, pad.top + plotH - y);
    const r = Math.min(4, barH / 2, barW / 2);
    const path = barH <= 0
      ? ""
      : `M${x} ${(pad.top + plotH).toFixed(1)} V${(y + r).toFixed(1)} Q${x} ${y.toFixed(1)} ${(x + r).toFixed(1)} ${y.toFixed(1)} H${(x + barW - r).toFixed(1)} Q${(x + barW).toFixed(1)} ${y.toFixed(1)} ${(x + barW).toFixed(1)} ${(y + r).toFixed(1)} V${(pad.top + plotH).toFixed(1)} Z`;
    const label = item.oz > 0 && item.oz === peak ? `<text class="board-value" x="${(x + barW / 2).toFixed(1)}" y="${(y - 6).toFixed(1)}" text-anchor="middle">${item.oz.toFixed(1)}</text>` : "";
    const detail = `${item.label}: ${item.oz.toFixed(1)} fl oz over ${item.count} ${item.count === 1 ? "pour" : "pours"}`;
    return `<g class="board-bar-group"><rect class="board-hit" data-board-bar="${escapeHtml(detail)}" tabindex="0" role="img" aria-label="${escapeHtml(detail)}" x="${(pad.left + band * index).toFixed(1)}" y="${pad.top}" width="${band.toFixed(1)}" height="${plotH}"></rect><path class="board-bar" d="${path}"></path>${label}${index % 2 === (series.length % 2 === 0 ? 1 : 0) ? `<text class="board-tick" x="${(x + barW / 2).toFixed(1)}" y="${height - 10}" text-anchor="middle">${escapeHtml(item.label)}</text>` : ""}</g>`;
  }).join("");
  return `<svg class="board-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Fluid ounces poured per day, last ${series.length} days"><text class="board-axis-title" x="${pad.left}" y="12">fl oz per day</text>${grid}${bars}<line class="board-axis" x1="${pad.left}" x2="${width - pad.right}" y1="${pad.top + plotH}" y2="${pad.top + plotH}"></line></svg>`;
}

function boardPeopleRows(pours, participants) {
  const totals = new Map();
  for (const pour of pours) {
    if (!pour.participant_id) continue;
    const entry = totals.get(pour.participant_id) || { oz: 0, count: 0, last: null };
    entry.oz += pour.volume_ml === null || pour.volume_ml === undefined ? 0 : flOz(pour.volume_ml);
    entry.count += 1;
    if (!entry.last || pour.ended_at > entry.last) entry.last = pour.ended_at;
    totals.set(pour.participant_id, entry);
  }
  return participants.map((person) => {
    const stats = totals.get(person.id) || { oz: 0, count: 0, last: null };
    const balance = Number(person.balance_cents || 0);
    const standing = balance < 0 ? "owes" : balance > 0 ? "credit" : "even";
    return { ...person, ...stats, balance, standing };
  }).sort((a, b) => a.balance - b.balance || b.oz - a.oz);
}

function displayView() {
  const s = state.snapshot;
  const inventory = s.inventory;
  const keg = s.keg;
  const percent = inventory ? Math.max(0, Math.min(100, decimal(inventory.percent_remaining))) : 0;
  const phase = String(s.device?.status?.state || "").toLowerCase();
  const pouring = ["pouring", "settling"].includes(phase);
  const last = s.last_pour;
  const pours = state.boardPours || [];
  const tab = BOARD_TABS.some(([key]) => key === state.boardTab) ? state.boardTab : "keg";
  const tabs = BOARD_TABS.map(([key, label]) => `<button class="board-tab ${tab === key ? "active" : ""}" role="tab" aria-selected="${tab === key}" data-action="board-tab" data-tab="${key}">${label}</button>`).join("");

  let panel = "";
  if (tab === "keg") {
    panel = `<div class="display-grid">
      <section class="display-keg">
        <p class="display-kicker">${escapeHtml(keg?.label || "No keg")}</p>
        <p class="display-metric">${percent.toFixed(0)}<span class="display-unit">%</span></p>
        <progress class="progress display-progress" max="100" value="${percent}" aria-label="Keg percent remaining">${percent}%</progress>
        <p class="display-sub">${inventory ? `${formatVolume(inventory.remaining_ml)} left \u00b7 \u2248${beersLeft(inventory.remaining_ml)} beers \u00b7 ${formatVolume(inventory.poured_ml)} poured` : "No keg installed"}</p>
      </section>
      <section class="display-live">
        <p class="display-kicker">${pouring ? "Live pour" : "Last pour"}</p>
        <p class="display-live-amount">${pouring && s.live_volume_ml !== null && s.live_volume_ml !== undefined
          ? escapeHtml(pourMeasurementText(s.live_volume_ml, s.active_calibration?.default_density_g_per_ml))
          : last ? escapeHtml(pourMeasurementText(last.volume_ml, last.calibration_density_g_per_ml)) : "\u2014"}</p>
        <p class="display-sub">${pouring
          ? `${escapeHtml(s.session?.participant_id ? (s.participants.find((item) => item.id === s.session.participant_id)?.display_name || "Someone") : "Guest")} is pouring`
          : last ? `${escapeHtml(last.participant_name || "Guest")} \u00b7 ${formatTime(last.ended_at)}` : "No pours recorded yet."}</p>
      </section>
    </div>`;
  } else if (tab === "pours") {
    const series = boardDailySeries(pours);
    const recent = pours.slice(0, 12);
    panel = `<section class="display-panel">
      <p class="display-kicker">Last 14 days</p>
      ${state.boardPours === null ? '<p class="muted">Loading pours\u2026</p>' : boardColumnChart(series)}
      <table class="board-table"><caption class="visually-hidden">Recent pours</caption><thead><tr><th>When</th><th>Who</th><th>Amount</th></tr></thead><tbody>${recent.length ? recent.map((pour) => `<tr><td>${formatTime(pour.ended_at)}</td><td>${escapeHtml(pour.participant_name || "Unrecorded")}</td><td class="num">${pour.volume_ml === null || pour.volume_ml === undefined ? `${escapeHtml(pour.raw_pulses)} pulses` : escapeHtml(pourMeasurementText(pour.volume_ml, pour.calibration_density_g_per_ml))}</td></tr>`).join("") : '<tr><td colspan="3">No pours yet.</td></tr>'}</tbody></table>
    </section>`;
  } else if (tab === "people") {
    const rows = boardPeopleRows(pours, s.participants || []);
    const septemberLeaders = septemberTopDrinkers(pours, s.participants || []);
    const maxOz = Math.max(...rows.map((row) => row.oz), 1);
    panel = `<section class="display-panel">
      <p class="display-kicker">September Top 5 Drinkers</p>
      <table class="board-table board-leaderboard"><caption class="visually-hidden">September top five drinkers since the counter reset</caption><thead><tr><th>Rank</th><th>Person</th><th>Drinks</th></tr></thead><tbody>${septemberLeaders.length ? septemberLeaders.map((row, index) => `<tr><td class="num">${index + 1}</td><td><span class="board-person"><span class="participant-avatar" aria-hidden="true">${participantAvatarMarkup(row)}</span>${escapeHtml(row.display_name)}</span></td><td class="num">${row.septemberDrinks} ${row.septemberDrinks === 1 ? "drink" : "drinks"}</td></tr>`).join("") : '<tr><td colspan="3">No profiles yet.</td></tr>'}</tbody></table>
      <p class="display-sub">Counter reset today. Tied drink counts are alphabetical.</p>
      <p class="display-kicker">All-time activity and balances</p>
      <table class="board-table board-people"><caption class="visually-hidden">People, pours, and balances</caption><thead><tr><th>Person</th><th>Poured</th><th>Pours</th><th>Standing</th></tr></thead><tbody>${rows.length ? rows.map((row) => `<tr class="standing-${row.standing}"><td><span class="board-person"><span class="participant-avatar" aria-hidden="true">${participantAvatarMarkup(row)}</span>${escapeHtml(row.display_name)}</span></td><td class="num"><span class="board-inline-bar" aria-hidden="true"><span style="width:${((row.oz / maxOz) * 100).toFixed(1)}%"></span></span>${row.oz.toFixed(1)} fl oz</td><td class="num">${row.count}</td><td class="num board-standing">${row.standing === "owes" ? `Owes ${formatMoney(-row.balance)}` : row.standing === "credit" ? `Credit ${formatMoney(row.balance)}` : "Paid up"}</td></tr>`).join("") : '<tr><td colspan="4">No profiles yet.</td></tr>'}</tbody></table>
    </section>`;
  } else {
    const photoFor = new Map((s.unattributed_pours || []).map((item) => [item.id, item.photo_id]));
    const unrecorded = pours.filter((pour) => !pour.participant_id).slice(0, 24);
    panel = `<section class="display-panel">
      <p class="display-kicker">Pours nobody has claimed</p>
      ${unrecorded.length ? `<div class="unattributed-row">${unrecorded.map((pour) => `<article class="unattributed-card">${photoFor.get(pour.id) ? `<img class="unattributed-photo" src="/api/v1/evidence/photos/${encodeURIComponent(photoFor.get(pour.id))}" alt="Snapshot from this pour" loading="lazy">` : '<div class="unattributed-photo placeholder" aria-hidden="true">CAM</div>'}<div class="unattributed-copy"><strong>${pour.volume_ml === null || pour.volume_ml === undefined ? `${escapeHtml(pour.raw_pulses)} pulses` : escapeHtml(pourMeasurementText(pour.volume_ml, pour.calibration_density_g_per_ml))}</strong><p class="muted">${formatTime(pour.ended_at)}</p></div></article>`).join("")}</div>` : '<p class="empty">Every pour has a name.</p>'}
    </section>`;
  }

  return `<section class="display-board ${pouring ? "flowing" : ""}">
    <header class="display-header">
      <h1 tabindex="-1">${escapeHtml(keg?.label || "KegPulse")}</h1>
      <p class="display-status">${pouring ? "Pouring now" : s.connection?.state === "connected" ? "On tap" : "Tap offline"}</p>
    </header>
    <nav class="board-tabs" role="tablist" aria-label="Tracking board">${tabs}</nav>
    ${panel}
  </section>`;
}

function participantsView() {
  if (state.security?.pin_configured && !state.security?.authenticated) {
    return page("Participants", "Editing profiles requires the administrator PIN.", `<section class="card narrow-card">${loginFormMarkup(true)}</section>`);
  }
  return page("Participants", "Profiles can be renamed or deactivated; historical pours are never deleted.", `
    <div class="grid two"><section class="card"><h2>Add participant</h2><form id="participant-form" class="stack"><label>Display name<input name="display_name" maxlength="80" autocomplete="off" required></label><button>Add participant</button></form></section>
    <section class="card"><h2>Profiles</h2><div id="participant-list" class="stack">${state.participantDetails ? participantList(state.participantDetails) : '<button class="secondary" data-action="load-participants">Load all profiles</button>'}</div></section></div>`);
}

function participantList(items) {
  if (!items.length) return '<p class="empty">No profiles.</p>';
  return items.map((item) => `<form class="participant-edit card" data-id="${escapeHtml(item.id)}"><label>Display name<input name="display_name" maxlength="80" value="${escapeHtml(item.display_name)}" required></label><label><input name="active" type="checkbox" ${item.active ? "checked" : ""}> Active on home screen</label><button class="secondary">Save profile</button></form>`).join("");
}

function settingsView() {
  if (state.security?.pin_configured && !state.security?.authenticated) {
    return page("Settings locked", "Device settings require the administrator PIN.", `<section class="card narrow-card">${loginFormMarkup(true)}</section>`);
  }
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
    <div class="grid two"><section class="card"><h2>Administrator PIN</h2><p>${state.security?.pin_configured ? "A PIN protects administrative actions." : "No PIN is configured. Anyone with physical access to this loopback kiosk can administer it."}</p><p id="admin-auth-status" class="${state.security?.authenticated ? "good-text" : "warning-text"}" role="status">${state.security?.authenticated ? "Administrator unlocked for this session." : "Administrator locked."}</p><form id="pin-form" class="stack" data-pin-form><span class="field-label">${state.security?.pin_configured ? "New PIN" : "PIN"}</span><button type="button" class="pin-display" data-action="open-keypad" aria-label="${state.security?.pin_configured ? "New PIN" : "PIN"}">Tap to enter PIN</button><input type="hidden" name="pin" autocomplete="off"><button>${state.security?.pin_configured ? "Change PIN" : "Set PIN"}</button></form><div id="admin-login-slot">${state.security?.pin_configured && !state.security?.authenticated ? loginFormMarkup(true) : ""}</div></section>
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
    } else if (lanLoginRequired()) {
      main.innerHTML = loginView();
      bindForms();
      main.querySelector("h1")?.focus({ preventScroll: true });
      state.renderedRoute = "__login__";
    }
    syncHostControls();
    return;
  }
  const loginRequired = lanLoginRequired();
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
    else if (current === "/calibration") {
      main.innerHTML = calibrationView();
      if (
        state.calibrationDetails === null
        && !state.calibrationLoadFailed
        && !state.pending.has("load-calibrations")
      ) {
        state.pending.add("load-calibrations");
        void loadCalibrations().finally(() => state.pending.delete("load-calibrations"));
      }
    }
    else if (current === "/display") {
      main.innerHTML = displayView();
      if (boardPoursStale() && !state.pending.has("board-pours")) {
        state.pending.add("board-pours");
        void loadBoardPours().finally(() => state.pending.delete("board-pours"));
      }
    }
    else if (current === "/participants") main.innerHTML = participantsView();
    else if (current === "/management") main.innerHTML = managementView();
    else if (current === "/settings") main.innerHTML = settingsView();
    else main.innerHTML = page("Not found", "That screen does not exist.", '<a class="button" href="#/">Return home</a>');
  }
  mountDemoGuide();
  bindForms();
  attachCameraPreview();
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
    void captureAvatarIfMissing(participantId);
    navigate("/pour");
  } catch (error) { showError(error); }
}

async function cancelPour() {
  const pulses = decimal(state.snapshot?.device?.status?.pulses);
  if (pulses > 0) {
    const accepted = await confirmAction("Counted pulses will be retained and saved as an interrupted partial pour. End now?", "End and save partial");
    if (!accepted) return;
  }
  const cancelling = state.snapshot?.session?.session_id || null;
  try {
    await mutation("cancel", "/api/v1/sessions/cancel");
    // A stale snapshot can still show this session arming for a moment; make
    // sure it cannot drag the kiosk back onto the pour screen.
    state.cancelledSessionId = cancelling;
    // A pending calibration sample must not hijack the screen after a cancel.
    navigate("/");
  } catch (error) { showError(error); }
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
    state.calibrationLoadFailed = false;
    if (route() === "/calibration") render();
  } catch (error) {
    state.calibrationLoadFailed = true;
    showError(error);
  }
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
    const pin = String(form.get("pin") || "");
    event.currentTarget.querySelector("input[name=pin]").value = "";
    if (!pin) { openKeypad(event.currentTarget); return; }
    if (state.pendingRelock) { try { await state.pendingRelock; } catch { /* relock already settled */ } }
    try {
      state.security = await api("/api/v1/security/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin }) });
      await refresh();
      syncSecurityUi();
      render();
      if (!state.socket) connectSocket();
      showToast("Administrator unlocked");
      if (route() === "/management") await loadManagement();
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
    const newPin = String(form.get("pin") || "");
    event.currentTarget.querySelector("input[name=pin]").value = "";
    if (!newPin) { openKeypad(event.currentTarget); return; }
    try { await mutation("pin", "/api/v1/security/pin", { pin: newPin }, "PUT"); state.security = await api("/api/v1/security/context"); showToast("Administrator PIN updated; unlock again with the new PIN"); render(); } catch (error) { showError(error); }
  });
  document.querySelector("#management-settings-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try {
      const saved = await mutation("management-settings", "/api/v1/management/settings", { price_per_fl_oz: form.get("price_per_fl_oz") }, "PATCH");
      if (saved) state.management = saved;
      showToast("Beer price saved"); render();
    } catch (error) { showError(error); }
  });
  document.querySelector("#keg-remaining-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    const accepted = await confirmAction(`Set the current keg to ${form.get("percent_remaining")}% remaining? This creates an audited inventory correction.`, "Update keg level");
    if (!accepted) return;
    try {
      await mutation("keg-remaining", "/api/v1/management/keg/remaining", { percent_remaining: form.get("percent_remaining"), reason: form.get("reason") });
      showToast("Keg level updated"); await loadManagement();
    } catch (error) { showError(error); }
  });
  for (const formElement of document.querySelectorAll(".fund-form")) formElement.addEventListener("submit", async (event) => {
    event.preventDefault(); const element = event.currentTarget; const form = new FormData(element);
    try {
      await mutation(`funds-${element.dataset.participant}`, `/api/v1/management/participants/${element.dataset.participant}/funds`, { amount_dollars: form.get("amount_dollars"), reason: form.get("reason") });
      showToast("Funds recorded"); await loadManagement();
    } catch (error) { showError(error); }
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
  if (action === "open-keypad") { openKeypad(button.closest("form")); return; }
  if (action === "arm") return arm(button.dataset.participant);
  if (action === "board-tab") {
    state.boardTab = button.dataset.tab;
    state.boardLastInteraction = Date.now();
    render();
    return;
  }
  if (action === "reload-page") { location.reload(); return; }
  if (action === "camera-test") return recordCameraTestClip();
  if (action === "discard-capture") {
    const accepted = await confirmAction("Discard this captured sample? Nothing is recorded and the calibration stays as it is.", "Discard sample");
    if (!accepted) return;
    try {
      await mutation("discard-capture", "/api/v1/calibrations/capture/discard");
      showToast("Sample discarded");
      if (route() === "/pour") navigate("/");
    } catch (error) { showError(error); }
    return;
  }
  if (action === "avatar-remove") {
    const accepted = await confirmAction("Remove this profile photo? A new one is captured automatically the next time they pour.", "Remove photo");
    if (accepted) {
      try {
        await mutation("avatar-remove", `/api/v1/participants/${button.dataset.participant}/avatar`, {}, "DELETE");
        showToast("Profile photo removed");
        await loadManagement();
        render();
      } catch (error) { showError(error); }
    }
    return;
  }
  if (action === "cancel") return cancelPour();
  if (action === "home-now") { state.completionPaused = false; return navigate("/"); }
  if (action === "stay") { pauseCompletionReturn(); return; }
  if (action === "load-history") return loadHistory();
  if (action === "show-reassign") return showReassign(button.dataset.pour);
  if (action === "cancel-reassign") { state.reassignPourId = null; render(); return; }
  if (action === "load-calibrations") { state.calibrationLoadFailed = false; return loadCalibrations(); }
  if (action === "capture-sample") return captureSample(button.dataset.calibration, button.dataset.ordinal);
  if (action === "toggle-sample") { try { await mutation(`sample-${button.dataset.ordinal}`, `/api/v1/calibrations/${button.dataset.calibration}/samples/${button.dataset.ordinal}`, { included: button.dataset.included === "1" }, "PATCH"); await loadCalibrations(); } catch (error) { showError(error); } return; }
  if (action === "activate-calibration") { const accepted = await confirmAction("Activate this reviewed factor? Historical pours will keep their original calibration.", "Activate calibration"); if (accepted) { try { await mutation("activate", `/api/v1/calibrations/${button.dataset.calibration}/activate`); showToast("Calibration activated"); await loadCalibrations(); } catch (error) { showError(error); } } return; }
  if (action === "activate-provisional-calibration") { const accepted = await confirmAction("Use the included samples as a temporary calibration? Volume, keg inventory, and user charges may be inaccurate until a full ten-sample calibration replaces it.", "Use temporary estimate"); if (accepted) { try { await mutation("activate-provisional", `/api/v1/calibrations/${button.dataset.calibration}/activate-provisional`); showToast("Provisional calibration activated"); await loadCalibrations(); } catch (error) { showError(error); } } return; }
  if (action === "start-verification") return startVerification();
  if (action === "load-participants") return loadParticipants();
  if (action === "load-management") return loadManagement();
  if (action === "camera-enable") return enableCamera();
  if (action === "camera-disable") return disableCamera();
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
document.addEventListener("change", async (event) => {
  const input = event.target?.classList?.contains("avatar-input") ? event.target : null;
  if (!input || !input.files?.length) return;
  const participantId = input.dataset.participant;
  const file = input.files[0];
  input.value = "";
  try {
    const blob = await jpegFromImageFile(file);
    await api("/api/v1/participants/" + encodeURIComponent(participantId) + "/avatar", { method: "PUT", headers: { "Content-Type": "image/jpeg" }, body: blob });
    showToast("Profile photo updated");
    await loadManagement();
    render();
  } catch (error) { showError(error); }
});

const keypadEntry = { value: "", formId: null };
const UI_BUILD = "2026-08-30.3";

function maybeReloadForNewBuild(snapshot) {
  const served = snapshot?.settings?.ui_build;
  if (!served || served === UI_BUILD) return;
  if (snapshot?.session) return; // never interrupt a pour in progress
  let already = null;
  try { already = sessionStorage.getItem("kegpulse-reloaded-for"); } catch { /* storage blocked */ }
  if (already === served) return;
  try { sessionStorage.setItem("kegpulse-reloaded-for", served); } catch { /* storage blocked */ }
  location.reload();
}
// ---- In-app on-screen keyboard ------------------------------------------
// Snap-browser accessibility auto-show keyboards are unreliable in kiosk
// mode, so the app carries its own. Shows for touch-focused editable fields
// on coarse-pointer devices (or when localStorage kegpulse-osk = "on";
// "off" disables it entirely).
const osk = document.querySelector("#osk");
const oskRows = document.querySelector("#osk-rows");
let oskTarget = null;
let oskShift = false;
let oskSymbols = false;

function oskPreference() {
  try { return localStorage.getItem("kegpulse-osk"); } catch { return null; }
}

function oskWanted() {
  const preference = oskPreference();
  if (preference === "off") return false;
  if (preference === "on") return true;
  return window.matchMedia("(pointer: coarse)").matches;
}

function oskIsNumeric(element) {
  if (element.type === "number") return true;
  const mode = (element.getAttribute("inputmode") || "").toLowerCase();
  return mode === "decimal" || mode === "numeric";
}

function oskEditable(element) {
  if (!element || element.closest("#keypad-dialog") || element.closest("#osk")) return null;
  if (element.tagName === "TEXTAREA") return element.readOnly ? null : element;
  if (element.tagName !== "INPUT") return null;
  const type = (element.type || "text").toLowerCase();
  if (!["text", "number", "search", "email", "url", "tel", "datetime-local"].includes(type)) return null;
  return element.readOnly || element.disabled ? null : element;
}

const OSK_LETTER_ROWS = [
  ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
  ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
  ["z", "x", "c", "v", "b", "n", "m"],
];
const OSK_SYMBOL_ROWS = [
  ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
  ["-", "/", ":", ";", "(", ")", "$", "&", "@"],
  [".", ",", "?", "!", "'", '"', "#", "%", "+"],
];

function oskKeyButton(label, action, classes) {
  const safe = escapeHtml(label);
  return `<button type="button" data-osk="${escapeHtml(action)}" class="${classes || ""}">${safe}</button>`;
}

function renderOsk() {
  if (!oskTarget) return;
  let rows;
  if (oskIsNumeric(oskTarget)) {
    rows = [
      ["7", "8", "9"].map((k) => oskKeyButton(k, `char:${k}`)).join(""),
      ["4", "5", "6"].map((k) => oskKeyButton(k, `char:${k}`)).join(""),
      ["1", "2", "3"].map((k) => oskKeyButton(k, `char:${k}`)).join(""),
      [oskKeyButton(".", "char:."), oskKeyButton("0", "char:0"), oskKeyButton("\u232b", "backspace")].join(""),
      [oskKeyButton("Hide", "hide", "osk-muted osk-wide"), oskKeyButton("\u21b5 Done", "enter", "osk-wide")].join(""),
    ];
  } else {
    const letters = (oskSymbols ? OSK_SYMBOL_ROWS : OSK_LETTER_ROWS).map((row) =>
      row.map((k) => {
        const ch = oskShift && !oskSymbols ? k.toUpperCase() : k;
        return oskKeyButton(ch, `char:${ch}`);
      }).join(""));
    rows = [
      letters[0],
      letters[1],
      [oskSymbols ? "" : oskKeyButton("\u21e7", "shift", oskShift ? "osk-wide" : "osk-wide osk-muted"), letters[2], oskKeyButton("\u232b", "backspace", "osk-wide")].join(""),
      [
        oskKeyButton(oskSymbols ? "ABC" : "?123", "symbols", "osk-muted osk-wide"),
        oskKeyButton("Space", "char: ", "osk-space"),
        oskKeyButton("Hide", "hide", "osk-muted"),
        oskKeyButton("\u21b5 Done", "enter", "osk-wide"),
      ].join(""),
    ];
  }
  oskRows.innerHTML = rows.map((row) => `<div class="osk-row">${row}</div>`).join("");
}

function showOsk(element) {
  oskTarget = element;
  oskShift = false;
  oskSymbols = false;
  renderOsk();
  osk.hidden = false;
  document.body.classList.add("osk-open");
}

function hideOsk() {
  oskTarget = null;
  osk.hidden = true;
  document.body.classList.remove("osk-open");
}

function oskInsert(text) {
  if (!oskTarget) return;
  oskTarget.focus();
  let inserted = false;
  try { inserted = document.execCommand("insertText", false, text); } catch { inserted = false; }
  if (!inserted) {
    oskTarget.value += text;
    oskTarget.dispatchEvent(new Event("input", { bubbles: true }));
  }
}

function oskBackspace() {
  if (!oskTarget) return;
  oskTarget.focus();
  let removed = false;
  try { removed = document.execCommand("delete", false); } catch { removed = false; }
  if (!removed) {
    oskTarget.value = oskTarget.value.slice(0, -1);
    oskTarget.dispatchEvent(new Event("input", { bubbles: true }));
  }
}

document.addEventListener("focusin", (event) => {
  if (!oskWanted()) return;
  const editable = oskEditable(event.target);
  if (editable) showOsk(editable);
  else if (!event.target.closest?.("#osk")) hideOsk();
});
document.addEventListener("focusout", (event) => {
  if (event.relatedTarget && (event.relatedTarget.closest?.("#osk") || oskEditable(event.relatedTarget))) return;
  window.setTimeout(() => {
    const active = document.activeElement;
    if (!active || (!active.closest?.("#osk") && !oskEditable(active))) hideOsk();
  }, 80);
});
osk.addEventListener("pointerdown", (event) => {
  // Keep focus on the input while tapping keys.
  if (event.target.closest("button")) event.preventDefault();
});
osk.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-osk]");
  if (!button || !oskTarget) return;
  const action = button.dataset.osk;
  if (action.startsWith("char:")) {
    oskInsert(action.slice(5));
    if (oskShift && !oskSymbols) { oskShift = false; renderOsk(); }
  } else if (action === "backspace") oskBackspace();
  else if (action === "shift") { oskShift = !oskShift; renderOsk(); }
  else if (action === "symbols") { oskSymbols = !oskSymbols; oskShift = false; renderOsk(); }
  else if (action === "enter") {
    const target = oskTarget;
    hideOsk();
    if (target.tagName === "TEXTAREA") target.blur();
    else if (target.form) { target.blur(); target.form.requestSubmit?.(); }
    else target.blur();
  } else if (action === "hide") hideOsk();
});

const keypadDialog = document.querySelector("#keypad-dialog");
const boardTooltip = document.createElement("div");
boardTooltip.id = "board-tooltip";
boardTooltip.hidden = true;
boardTooltip.setAttribute("role", "status");
document.body.append(boardTooltip);
function showBoardTooltip(target, x, y) {
  const detail = target?.dataset?.boardBar;
  if (!detail) { boardTooltip.hidden = true; return; }
  boardTooltip.textContent = detail;
  boardTooltip.hidden = false;
  const box = boardTooltip.getBoundingClientRect();
  boardTooltip.style.left = `${Math.max(8, Math.min(window.innerWidth - box.width - 8, x + 12))}px`;
  boardTooltip.style.top = `${Math.max(8, y - box.height - 12)}px`;
}
main.addEventListener("pointermove", (event) => {
  const target = event.target?.closest?.("[data-board-bar]");
  if (target) showBoardTooltip(target, event.clientX, event.clientY);
  else if (!boardTooltip.hidden && !document.activeElement?.dataset?.boardBar) boardTooltip.hidden = true;
});
main.addEventListener("pointerleave", () => { boardTooltip.hidden = true; });
main.addEventListener("focusin", (event) => {
  const target = event.target?.closest?.("[data-board-bar]");
  if (!target) return;
  const box = target.getBoundingClientRect();
  showBoardTooltip(target, box.left + box.width / 2, box.top);
});
main.addEventListener("focusout", () => { boardTooltip.hidden = true; });
window.setInterval(() => {
  if (route() !== "/display" || document.hidden) return;
  if (Date.now() - state.boardLastInteraction < 60_000) return;
  const index = BOARD_TABS.findIndex(([key]) => key === state.boardTab);
  state.boardTab = BOARD_TABS[(index + 1) % BOARD_TABS.length][0];
  render();
}, 20_000);
main.addEventListener("toggle", (event) => {
  const id = event.target?.dataset?.pourDetails;
  if (!id) return;
  state.openPourDetails ||= new Set();
  if (event.target.open) state.openPourDetails.add(id);
  else state.openPourDetails.delete(id);
}, true);
function keypadDots() {
  const el = document.querySelector("#keypad-dots");
  if (!el) return;
  el.textContent = keypadEntry.value ? "\u25cf".repeat(keypadEntry.value.length) : "Enter 4\u201320 digits";
  el.classList.toggle("empty", !keypadEntry.value);
}
function openKeypad(form) {
  if (keypadDialog.open) return;
  keypadEntry.value = "";
  keypadEntry.formId = form?.id || null;
  keypadDots();
  keypadDialog.showModal();
}
function keypadPress(key) {
  if (key === "cancel") { keypadEntry.value = ""; keypadEntry.formId = null; keypadDialog.close(); return; }
  if (key === "clear") { keypadEntry.value = ""; keypadDots(); return; }
  if (key === "back") { keypadEntry.value = keypadEntry.value.slice(0, -1); keypadDots(); return; }
  if (key === "ok") {
    if (keypadEntry.value.length < 4 || keypadEntry.value.length > 20) {
      const el = document.querySelector("#keypad-dots");
      el.textContent = "PIN must be 4\u201320 digits";
      el.classList.add("empty");
      return;
    }
    const formId = keypadEntry.formId;
    const pin = keypadEntry.value;
    keypadEntry.value = "";
    keypadEntry.formId = null;
    keypadDialog.close();
    const form = formId ? document.getElementById(formId) : null;
    if (form) {
      const hidden = form.querySelector("input[name=pin]");
      if (hidden) { hidden.value = pin; form.requestSubmit(); }
    } else {
      void submitKeypadLogin(pin);
    }
    return;
  }
  if (/^[0-9]$/.test(key) && keypadEntry.value.length < 20) { keypadEntry.value += key; keypadDots(); }
}
keypadDialog.addEventListener("click", (event) => {
  const key = event.target.closest("[data-key]")?.dataset.key;
  if (key) keypadPress(key);
});
keypadDialog.addEventListener("keydown", (event) => {
  if (/^[0-9]$/.test(event.key)) { event.preventDefault(); keypadPress(event.key); }
  else if (event.key === "Backspace") { event.preventDefault(); keypadPress("back"); }
  else if (event.key === "Enter") { event.preventDefault(); keypadPress("ok"); }
});
keypadDialog.addEventListener("cancel", () => { keypadEntry.value = ""; keypadEntry.formId = null; });
async function submitKeypadLogin(pin) {
  if (state.pendingRelock) { try { await state.pendingRelock; } catch { /* relock settled */ } }
  try {
    state.security = await api("/api/v1/security/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin }) });
    await refresh();
    syncSecurityUi();
    render();
    if (!state.socket) connectSocket();
    showToast("Administrator unlocked \u2014 tap that action again");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    showToast(message, true);
    announcer.textContent = `Error: ${message}`;
  }
}

function relockAdminOnLeave() {
  if (!state.security?.pin_configured || !state.security?.authenticated) return;
  state.security = { ...state.security, authenticated: false };
  state.pendingRelock = api("/api/v1/security/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .catch(() => {})
    .then(() => refreshSecurityContext())
    .catch(() => {})
    .finally(() => { state.pendingRelock = null; });
}
const ADMIN_ROUTES = new Set(["/management", "/settings", "/participants", "/calibration", "/keg"]);
// Transient pour screens: a calibration capture routes through them, so they
// neither require nor drop administrator access.
const NEUTRAL_ROUTES = new Set(["/pour", "/complete"]);
state.currentRoute = route();
window.addEventListener("hashchange", () => {
  const previousRoute = state.currentRoute;
  state.currentRoute = route();
  state.completionPaused = false;
  if (state.currentRoute !== "/complete") {
    window.clearTimeout(state.completionTimer);
    state.completionTimer = null;
  }
  if (
    ADMIN_ROUTES.has(previousRoute)
    && !ADMIN_ROUTES.has(state.currentRoute)
    && !NEUTRAL_ROUTES.has(state.currentRoute)
  ) {
    relockAdminOnLeave();
  }
  render();
  updateChrome();
  if (route() === "/history") void loadHistory();
  if (route() === "/management") void loadManagement();
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
    if (lanLoginRequired(state.security)) {
      enterLoginMode();
    } else {
      const ready = await refresh();
      if (ready) {
        if (route() === "/history") await loadHistory();
        if (route() === "/management") await loadManagement();
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
