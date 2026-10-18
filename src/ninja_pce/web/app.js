"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const views = {
    overview: ["Overview", "Controller overview", "Live topology and reported RSVP-TE state in one workspace."],
    topology: ["Topology", "Directed TE topology", "Inspect receiving interfaces and advertised RSVP reservations."],
    peers: ["Peers", "Peers and data sources", "Session state, source freshness and local reservation accounting."],
    lsps: ["LSPs", "Reported label switched paths", "Select a report to inspect its identity, bandwidth and explicit route."],
    activity: ["Activity", "Controller activity", "Captured protocol events and path computation outcomes."],
  };
  const state = { data: null, selected: null, view: "overview", lspFilter: "all", logFilter: "all", search: "", paused: false, loading: false, error: null, timer: null, receivedAt: 0, inspected: null };
  const string = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : String(value);
  const list = (value) => Array.isArray(value) ? value : [];
  const number = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  const titleCase = (value) => string(value, "Unknown").replaceAll("_", " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());
  const count = (value) => number(value) === null ? "—" : value.toLocaleString();
  const bandwidth = (value) => {
    if (number(value) === null) return "Unknown";
    if (value >= 1e9) return `${(value / 1e9).toLocaleString(undefined, { maximumFractionDigits: 3 })} Gbps`;
    if (value >= 1e6) return `${(value / 1e6).toLocaleString(undefined, { maximumFractionDigits: 3 })} Mbps`;
    if (value >= 1e3) return `${(value / 1e3).toLocaleString(undefined, { maximumFractionDigits: 3 })} kbps`;
    return `${value.toLocaleString()} bps`;
  };
  const date = (value) => { const parsed = new Date(value); return value && Number.isFinite(parsed.getTime()) ? parsed : null; };
  const time = (value) => date(value)?.toLocaleTimeString(undefined, { hour12: false }) || "—";
  const fullTime = (value) => date(value)?.toLocaleString(undefined, { hour12: false }) || "Unknown timestamp";
  const age = (value) => number(value) === null ? "Age unknown" : value < 60 ? `${Math.floor(value)}s ago` : value < 3600 ? `${Math.floor(value / 60)}m ago` : `${Math.floor(value / 3600)}h ago`;
  const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
  const svg = (tag, attributes = {}, text) => { const node = document.createElementNS(NS, tag); for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value)); if (text !== undefined) node.textContent = text; return node; };
  const cell = (row, text, className = "") => { const node = el("td", className); if (text instanceof Node) node.append(text); else node.textContent = string(text); row.append(node); return node; };
  const emptyTable = (body, columns, message) => { const row = el("tr"); const node = cell(row, message, "table-empty"); node.colSpan = columns; body.append(row); };
  const isActive = (lsp) => ["ACTIVE", "UP"].includes(string(lsp.reported_state || lsp.operational_state).toUpperCase());
  const selectedLsp = () => list(state.data?.lsps).find((lsp) => String(lsp.id) === state.selected) || null;
  const source = (id) => list(state.data?.sources).find((item) => item.id === id);
  const fresh = (item) => item?.fresh === true && !state.error;
  const sourceState = (item) => state.error && item?.state === "fresh" ? "stale" : string(item?.state, "unavailable");
  const dotClass = (value) => ["fresh", "up", "active", "established", "verified"].includes(String(value).toLowerCase()) ? "" : ["stale", "warning", "going-up", "going-down", "pending", "applied_unverified"].includes(String(value).toLowerCase()) ? "warn" : ["down", "unavailable", "invalid", "error", "verification_failed", "rollback_required"].includes(String(value).toLowerCase()) ? "off" : "unknown";
  const pill = (label, status = label) => { const node = el("span", "status-pill"); node.append(el("span", `dot ${dotClass(status)}`), el("span", "", titleCase(label))); return node; };
  const sourceAge = (item) => number(item.age_seconds) === null ? null : item.age_seconds + Math.max(0, (Date.now() - state.receivedAt) / 1000);

  function setTheme(theme) {
    document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
    const label = `Switch to ${theme === "light" ? "dark" : "light"} theme`;
    $("theme-toggle").setAttribute("aria-label", label);
    $("theme-toggle").title = label;
  }
  try { setTheme(localStorage.getItem("theme") || "dark"); } catch { setTheme("dark"); }
  $("theme-toggle").addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    setTheme(theme);
    try { localStorage.setItem("theme", theme); } catch { /* Theme remains usable without storage. */ }
  });

  function viewDescription(view) {
    return state.data?.mode === "demo" && view === "overview" ? "Explore a sample topology, tunnels and path computations." : views[view][2];
  }
  function navigate(view, updateHash = true) {
    state.view = Object.hasOwn(views, view) ? view : "overview";
    const [label, heading] = views[state.view];
    $("page-title").textContent = label;
    $("view-heading").textContent = heading;
    $("view-description").textContent = viewDescription(state.view);
    document.querySelectorAll("[data-view]").forEach((button) => {
      const active = button.dataset.view === state.view;
      button.classList.toggle("on", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-views]").forEach((section) => { section.hidden = !section.dataset.views.split(" ").includes(state.view); });
    if (updateHash && location.hash !== `#${state.view}`) history.replaceState(null, "", `#${state.view}`);
  }
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1), false));
  navigate(location.hash.slice(1) || "overview", false);

  function selectLsp(id) {
    state.selected = String(id);
    state.inspected = null;
    renderSelection();
  }
  document.querySelectorAll("[data-lsp-filter]").forEach((button) => button.addEventListener("click", () => {
    state.lspFilter = button.dataset.lspFilter;
    document.querySelectorAll("[data-lsp-filter]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
    renderLsps();
  }));
  document.querySelectorAll("[data-log-filter]").forEach((button) => button.addEventListener("click", () => {
    state.logFilter = button.dataset.logFilter;
    document.querySelectorAll("[data-log-filter]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
    renderEvents();
  }));
  $("lsp-search").addEventListener("input", (event) => { state.search = event.target.value.toLowerCase(); renderLsps(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) && !document.querySelector("dialog[open]")) {
      event.preventDefault();
      if (!["overview", "lsps"].includes(state.view)) navigate("lsps");
      $("lsp-search").focus();
    }
    if (event.key === "Escape" && document.activeElement === $("lsp-search")) { $("lsp-search").value = ""; state.search = ""; renderLsps(); }
  });
  $("info-toggle").addEventListener("click", () => $("info-dialog").showModal());
  $("info-close").addEventListener("click", () => $("info-dialog").close());

  const creation = { options: null, principal: null, pending: null, operations: [], busy: false, timer: null, plan: null, planIntent: null, previewBusy: false, previewTimer: null, expiryTimer: null, previewGeneration: 0, releasedMessage: null };
  function storedRequest(value) {
    return value && typeof value.key === "string" && value.intent && (!value.id || /^[0-9a-f]{32}$/.test(value.id));
  }
  function loadCreation(principal) {
    if (creation.principal !== principal) { creation.releasedMessage = null; $("creation-status").replaceChildren(); }
    creation.principal = principal; creation.pending = null; creation.operations = [];
    try {
      const saved = JSON.parse(sessionStorage.getItem(`ninja-pce-creations:${principal}`) || "null");
      if (storedRequest(saved?.pending) && saved.pending.principal === principal) creation.pending = saved.pending;
      if (Array.isArray(saved?.operations)) creation.operations = saved.operations.filter((item) => storedRequest(item) && item.id && item.principal === principal).slice(-32);
      if (!saved) {
        const legacy = JSON.parse(sessionStorage.getItem("ninja-pce-creation") || "null");
        if (storedRequest(legacy) && (!legacy.principal || legacy.principal === principal)) creation.pending = { ...legacy, unbound: !legacy.principal };
      }
    } catch { /* Never resubmit a stored request automatically. */ }
    renderCreationOperations();
  }
  function saveCreation() {
    if (!creation.principal) return;
    try { sessionStorage.setItem(`ninja-pce-creations:${creation.principal}`, JSON.stringify({ pending: creation.pending, operations: creation.operations })); } catch { /* In-memory idempotency remains intact. */ }
  }
  function renderCreationOperations() {
    $("creation-operations").hidden = !creation.operations.length;
    $("creation-operation-list").replaceChildren(...creation.operations.slice().reverse().map((request) => {
      const button = el("button", "button", `${request.intent.headend} → ${request.intent.destination} · ${request.status} · ${request.id}`);
      button.type = "button";
      button.disabled = creation.busy || Boolean(creation.pending && !creation.pending.id);
      button.addEventListener("click", () => {
        if (creation.busy || creation.pending && !creation.pending.id) return;
        creation.pending = { ...request }; saveCreation(); invalidatePreview();
        $("creation-headend").value = request.intent.headend; $("creation-destination").value = request.intent.destination;
        $("creation-bandwidth").value = String(request.intent.bandwidth_bps / 1e6);
        if (request.plan) showPlan(request.plan, true);
        creationControls(); checkCreation();
      });
      return button;
    }));
  }
  function creationMessage(message, operation = null) {
    const target = $("creation-status"); target.replaceChildren(el("p", "", message));
    if (!operation && creation.releasedMessage) target.prepend(el("p", "", creation.releasedMessage));
    if (operation) {
      target.append(el("span", "mono", `Operation ${operation.id}`));
      if (operation.reported?.operational_state) target.append(el("p", "", `Router reports ${operation.reported.operational_state}.`));
      target.append(el("p", "muted", "Traffic forwarding has not been verified."));
      if (operation.reason) target.append(el("p", "", String(operation.reason)));
      if (Array.isArray(operation.path_nodes)) target.append(el("p", "mono", operation.path_nodes.join(" → ")));
    }
  }
  function creationControls() {
    const held = Boolean(creation.pending);
    const planRequired = creation.options?.requires_plan === true;
    const usablePlan = creation.plan && date(creation.plan.expires_at)?.getTime() > Date.now();
    const backendMismatch = held && (planRequired !== Boolean(creation.pending.approved_plan_id) || creation.pending.unbound);
    const recovery = held && !creation.pending.id && planRequired && /^[0-9a-f]{64}$/.test(creation.pending.approved_plan_id);
    $("creation-fields").disabled = creation.busy || held || !creation.options?.headends.length;
    $("creation-submit").disabled = creation.busy || Boolean(creation.pending?.id) || !selectedHeadendReady() && !recovery || backendMismatch || !held && planRequired && !usablePlan;
    $("creation-submit").hidden = Boolean(creation.pending?.id);
    $("creation-submit").textContent = creation.busy ? "Submitting…" : held ? "Retry same request" : "Create tunnel";
    $("creation-check").hidden = !creation.pending?.id;
    $("creation-check").disabled = creation.busy;
    $("creation-new").hidden = !creation.pending?.id;
    $("creation-new").disabled = creation.busy;
    renderCreationOperations();
    const readiness = creation.options?.headend_readiness?.[$("creation-headend").value];
    $("creation-readiness").textContent = readiness ? [
      readiness.transport_enrolled === true ? "Transport enrolled." : readiness.transport_enrolled === false ? "Transport enrollment required." : "",
      readiness.transport_ready === false && readiness.transport_enrolled === true ? "Transport identity unavailable." : "",
      readiness.pcc_ready === true ? "PCC synchronized." : readiness.pcc_ready === false ? "PCC unavailable." : "",
      String(readiness.reason || "")
    ].filter(Boolean).join(" ") : "";
  }
  function selectedHeadendReady() {
    if (creation.options?.enabled !== true) return false;
    const states = creation.options.headend_readiness;
    return !states || states[$("creation-headend").value]?.enabled === true;
  }
  function creationIntent() {
    const amount = $("creation-bandwidth").value;
    if (!/^\d+(?:\.\d{1,6})?$/.test(amount)) throw new Error("Enter a positive bandwidth with up to six decimal places.");
    const [whole, fraction = ""] = amount.split(".");
    const bps = Number(BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, "0")));
    if (!Number.isSafeInteger(bps) || bps <= 0 || bps > creation.options.max_bandwidth_bps) throw new Error("Bandwidth exceeds the operator policy.");
    const headend = $("creation-headend").value, destination = $("creation-destination").value;
    if (headend === destination || !creation.options.headends.includes(headend) || !creation.options.destinations.includes(destination)) throw new Error("Choose a different authorized destination.");
    return { headend, destination, bandwidth_bps: bps };
  }
  function showPlan(plan, previous = false) {
    $("creation-preview").hidden = !plan;
    if (!plan) return;
    $("creation-preview-path").textContent = list(plan.path_nodes).join(" → ");
    const requested = creation.planIntent?.bandwidth_bps || creation.pending?.intent?.bandwidth_bps;
    $("creation-preview-bandwidth").textContent = `RSVP bandwidth: ${bandwidth(plan.bandwidth_bps)}${requested && requested !== plan.bandwidth_bps ? ` (rounded up from ${bandwidth(requested)})` : ""}.`;
    $("creation-preview-config").textContent = String(plan.rendered_config || "");
    $("creation-preview-expiry").textContent = previous ? "Configuration approved for the previous submission." : `Valid until ${fullTime(plan.expires_at)}. Create applies exactly this plan.`;
  }
  function invalidatePreview() {
    clearTimeout(creation.previewTimer); clearTimeout(creation.expiryTimer);
    creation.previewGeneration += 1; creation.previewBusy = false;
    creation.plan = null; creation.planIntent = null; showPlan(null);
  }
  function queuePreview() {
    if (creation.pending) return;
    invalidatePreview(); creationControls();
    if (!selectedHeadendReady() || creation.options.requires_plan !== true || !$("create-tunnel-dialog").open) return;
    creationMessage("Preparing the configuration preview…");
    creation.previewTimer = setTimeout(previewCreation, 350);
  }
  async function previewCreation() {
    if (creation.pending || creation.busy || creation.options?.requires_plan !== true || !selectedHeadendReady()) return;
    const generation = creation.previewGeneration;
    let intent;
    try { intent = creationIntent(); } catch (error) { creationMessage(error.message); return; }
    creation.previewBusy = true; creationControls();
    try {
      const result = await creationFetch("/api/tunnel-plans", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": creation.options.csrf_token }, body: JSON.stringify(intent) });
      if (generation !== creation.previewGeneration || creation.pending || !$("create-tunnel-dialog").open) return;
      const plan = result.plan;
      if (!plan || !/^[0-9a-f]{64}$/.test(plan.plan_id) || date(plan.expires_at)?.getTime() <= Date.now() || !date(plan.expires_at) || typeof plan.rendered_config !== "string" || !plan.rendered_config || !Array.isArray(plan.path_nodes) || !plan.path_nodes.length || plan.path_nodes.some((name) => typeof name !== "string") || ["headend", "destination"].some((key) => plan[key] !== intent[key]) || !Number.isSafeInteger(plan.bandwidth_bps) || plan.bandwidth_bps < intent.bandwidth_bps || plan.bandwidth_bps - intent.bandwidth_bps >= 1000 || plan.bandwidth_bps > creation.options.max_bandwidth_bps) throw new Error("Unsupported or expired configuration preview.");
      creation.plan = plan; creation.planIntent = intent; showPlan(plan);
      creationMessage("Review the path and configuration, then select Create tunnel.");
      creation.expiryTimer = setTimeout(queuePreview, Math.max(1, date(plan.expires_at).getTime() - Date.now()));
    } catch (error) { if (generation === creation.previewGeneration) creationMessage(error.message); }
    finally { if (generation === creation.previewGeneration) { creation.previewBusy = false; creationControls(); } }
  }
  async function creationFetch(path, options = {}) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path, { cache: "no-store", credentials: "same-origin", signal: controller.signal, ...options, headers: { Accept: "application/json", ...options.headers } });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || (response.status === 401 || response.status === 403 ? "Authenticated access is required." : `Creation service unavailable (${response.status}).`));
      return data;
    } finally { clearTimeout(timeout); }
  }
  function showOperation(operation) {
    if (!operation || !/^[0-9a-f]{32}$/.test(operation.id) || typeof operation.status !== "string") throw new Error("Unsupported operation response.");
    if (operation.principal && operation.principal !== creation.principal) throw new Error("Operation belongs to a different authenticated principal.");
    if (operation.status === "VERIFIED" || operation.verified === true) {
      const proof = operation.verification, checks = ["identity", "destination", "bandwidth", "ero", "signaling"];
      if (creation.options?.requires_plan !== true || proof?.independent !== true || proof.scope !== "headend-rsvp" || proof.pcep_control !== true || !Array.isArray(proof.checks) || proof.checks.length !== checks.length || !checks.every((check) => proof.checks.includes(check))) throw new Error("Independent RSVP verification is unavailable.");
    }
    creation.pending.id = operation.id; creation.pending.status = operation.status;
    creation.pending.principal = creation.principal; delete creation.pending.unbound;
    creation.operations = creation.operations.filter((request) => request.id !== operation.id);
    creation.operations.push({ ...creation.pending }); creation.operations = creation.operations.slice(-32); saveCreation();
    const messages = {
      PLANNED: "Path admitted. Waiting for delivery.", PUSHING: "Request sent. Waiting for the router’s response.",
      APPLIED_UNVERIFIED: "Router acknowledged the new LSP. Independent verification is pending.",
      ROLLBACK_REQUIRED: "Delivery needs reconciliation. Capacity remains held; this request will not be sent again.",
      REJECTED: "Request rejected before delivery. No tunnel was created by this operation.",
      ROLLED_BACK: "Owned tunnel removal and resource release were confirmed.",
      CREATED: "Tunnel configuration created. Waiting for independent RSVP checks.",
      VERIFIED: "RSVP verified on the headend. Traffic forwarding remains unverified.", VERIFICATION_FAILED: "Tunnel verification failed. Capacity remains held.",
    };
    creationMessage(messages[operation.status] || `Operation state: ${operation.status}`, operation);
    if (["REJECTED", "ROLLED_BACK"].includes(operation.status)) {
      creation.releasedMessage = `${messages[operation.status]}${operation.reason ? ` ${operation.reason}` : ""}`;
      creation.pending = null; saveCreation(); queuePreview();
    }
    clearTimeout(creation.timer);
    if ((["PLANNED", "PUSHING", "CREATED"].includes(operation.status) || operation.status === "APPLIED_UNVERIFIED" && (creation.options?.requires_plan === true || !["ACTIVE", "UP"].includes(operation.reported?.operational_state))) && $("create-tunnel-dialog").open) creation.timer = setTimeout(checkCreation, 2000);
    creationControls();
  }
  async function checkCreation() {
    const pending = creation.pending;
    if (!pending?.id || creation.busy) return;
    const key = pending.key, id = pending.id, principal = creation.principal;
    const current = () => creation.principal === principal && creation.pending?.key === key && creation.pending?.id === id;
    try {
      const result = await creationFetch(`/api/tunnel-operations/${id}`);
      if (!current()) return;
      if (result.operation?.id !== id) throw new Error("Operation status does not match the requested identity.");
      showOperation(result.operation);
    } catch (error) { if (current()) creationMessage(`${error.message} Keep this operation ID for reconciliation: ${id}`); }
  }
  async function openCreation() {
    $("create-tunnel-dialog").showModal();
    invalidatePreview();
    if (!creation.pending) $("creation-status").replaceChildren();
    creation.options = null; creationControls(); $("creation-operations").hidden = true;
    if (state.data?.mode === "demo") { $("creation-policy").textContent = "This sample is for exploring the dashboard. Connect and configure your network to create tunnels."; return; }
    $("creation-policy").textContent = "Checking authenticated access and domain policy…";
    try {
      const options = await creationFetch("/api/tunnel-options");
      if (!options || !Array.isArray(options.headends) || !Array.isArray(options.destinations) || typeof options.enabled !== "boolean") throw new Error("Unsupported creation policy.");
      if (options.enabled === false && !options.headends.length && !options.principal) {
        creation.principal = null; creation.pending = null; creation.operations = []; creation.options = options;
        $("creation-policy").textContent = String(options.reason || "Tunnel creation is unavailable.");
        creationControls(); return;
      }
      if (typeof options.principal !== "string" || !/^[A-Za-z0-9_.@-]{1,128}$/.test(options.principal)) throw new Error("Authenticated principal is unavailable.");
      loadCreation(options.principal); creation.options = options;
      for (const [id, names] of [["creation-headend", options.headends], ["creation-destination", options.destinations]]) {
        $(id).replaceChildren(...names.filter((name) => typeof name === "string").map((name) => { const option = el("option", "", name); option.value = name; return option; }));
      }
      if (!creation.pending && options.headend_readiness) $("creation-headend").value = options.headends.find((name) => options.headend_readiness[name]?.enabled === true) || options.headends[0] || "";
      if (number(options.max_bandwidth_bps) !== null) $("creation-bandwidth").max = String(options.max_bandwidth_bps / 1e6);
      if (!creation.pending && $("creation-headend").value === $("creation-destination").value) $("creation-destination").value = options.destinations.find((name) => name !== $("creation-headend").value) || "";
      $("creation-policy").textContent = options.enabled ? `${options.requires_plan === true ? "Router configuration + PCEP path control" : "PCEP initiation"} · RSVP-TE · priorities 7/7 · maximum ${bandwidth(options.max_bandwidth_bps)}` : String(options.reason || "Tunnel creation is unavailable for this headend.");
      if (creation.pending) {
        $("creation-headend").value = creation.pending.intent.headend;
        $("creation-destination").value = creation.pending.intent.destination;
        $("creation-bandwidth").value = String(creation.pending.intent.bandwidth_bps / 1e6);
        if (creation.pending.plan) showPlan(creation.pending.plan, true);
        if (creation.pending.id) await checkCreation();
        else if (creation.pending.unbound) creationMessage("The previous browser request has no authenticated principal binding. Keep its idempotency key for controlled reconciliation before creating another tunnel.");
        else if ((options.requires_plan === true) !== Boolean(creation.pending.approved_plan_id)) creationMessage("The previous request belongs to a different provisioning backend. Keep its idempotency key for reconciliation; it cannot be resubmitted here.");
        else creationMessage("A previous submission has an unknown outcome. Retry the same request to recover its operation; do not submit a different intent.");
      }
      else queuePreview();
    } catch (error) { $("creation-policy").textContent = error.message; }
    creationControls();
  }
  $("create-tunnel-button").addEventListener("click", openCreation);
  $("create-tunnel-close").addEventListener("click", () => $("create-tunnel-dialog").close());
  $("create-tunnel-dialog").addEventListener("close", () => { if (!$("create-tunnel-dialog").open) { clearTimeout(creation.timer); invalidatePreview(); } });
  $("creation-fields").addEventListener("input", queuePreview);
  $("creation-fields").addEventListener("change", queuePreview);
  $("creation-check").addEventListener("click", checkCreation);
  $("creation-new").addEventListener("click", () => {
    if (creation.busy || !creation.pending?.id) return;
    clearTimeout(creation.timer); creation.pending = null; creation.releasedMessage = null;
    if (!creation.options.headends.includes($("creation-headend").value)) $("creation-headend").value = creation.options.headends.find((name) => !creation.options.headend_readiness || creation.options.headend_readiness[name]?.enabled === true) || creation.options.headends[0] || "";
    if (!creation.options.destinations.includes($("creation-destination").value) || $("creation-headend").value === $("creation-destination").value) $("creation-destination").value = creation.options.destinations.find((name) => name !== $("creation-headend").value) || "";
    saveCreation(); queuePreview();
  });
  $("create-tunnel-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const recovery = creation.pending && !creation.pending.id && creation.options?.requires_plan === true && /^[0-9a-f]{64}$/.test(creation.pending.approved_plan_id);
    if (creation.busy || !selectedHeadendReady() && !recovery || creation.pending?.id || creation.pending?.unbound) return;
    if (!creation.pending) {
      let intent;
      try { intent = creationIntent(); } catch (error) { creationMessage(error.message); return; }
      if (creation.options.requires_plan === true && (!creation.plan || date(creation.plan.expires_at)?.getTime() <= Date.now() || JSON.stringify(intent) !== JSON.stringify(creation.planIntent))) { queuePreview(); return; }
      creation.pending = { key: crypto.randomUUID(), principal: creation.principal, intent };
      if (creation.options.requires_plan === true) { creation.pending.approved_plan_id = creation.plan.plan_id; creation.pending.plan = creation.plan; }
      saveCreation();
    }
    if ((creation.options.requires_plan === true) !== Boolean(creation.pending.approved_plan_id)) return;
    creation.releasedMessage = null;
    const principal = creation.principal, key = creation.pending.key;
    creation.busy = true; creationControls(); creationMessage("Computing and admitting the requested path…");
    try {
      const payload = { ...creation.pending.intent };
      if (creation.pending.approved_plan_id) payload.approved_plan_id = creation.pending.approved_plan_id;
      const result = await creationFetch("/api/tunnels", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": creation.options.csrf_token, "Idempotency-Key": creation.pending.key }, body: JSON.stringify(payload) });
      if (creation.principal !== principal || creation.pending?.key !== key) return;
      showOperation(result.operation);
    } catch (error) { if (creation.principal === principal && creation.pending?.key === key) creationMessage(`${error.message} The outcome is unknown. Retry the same request to recover its operation.`); }
    finally { creation.busy = false; creationControls(); }
  });

  function renderChrome() {
    const data = state.data;
    const mode = data?.mode === "demo" ? "Sample" : data?.operating_mode === "observe" ? "Observe" : data?.operating_mode === "pcep-control" ? "PCEP control" : null;
    const readiness = data?.readiness;
    const readinessLabel = state.error ? "Readiness unavailable" : state.paused ? "Readiness paused" : titleCase(readiness?.state);
    $("readiness-banner").hidden = !readiness || data?.mode === "demo";
    $("readiness-state").textContent = readinessLabel;
    const startupFindings = list(readiness?.startup_findings).map((finding) => `${string(finding.code)}: ${string(finding.message)} ${string(finding.remediation)}`).join(" ");
    $("readiness-message").textContent = state.error || state.paused ? "The retained snapshot does not confirm current readiness." : `${string(readiness?.message, "No readiness details reported.")}${startupFindings ? ` ${startupFindings}` : ""}`;
    $("readiness-dot").className = `dot ${state.error ? "off" : state.paused || !readiness?.ready ? "warn" : ""}`;
    $("operating-mode").textContent = mode || "Mode unknown";
    $("demo-banner").hidden = data?.mode !== "demo";
    $("view-description").textContent = viewDescription(state.view);
    $("activity-feed-note").textContent = data?.mode === "demo" ? "Synthetic sample events" : "Captured controller events";
    $("error-banner").hidden = !state.error;
    $("error-banner").textContent = state.error ? `${state.error}${data ? " Showing the last received snapshot; its freshness is no longer confirmed." : " No controller data is displayed."}` : "";
    const connection = $("connection-status");
    connection.replaceChildren(el("span", `dot ${state.error ? "off" : data ? state.paused ? "warn" : "" : "unknown"}`), el("span", "", state.error ? "Unavailable" : state.paused ? "Paused" : data?.mode === "demo" ? "Demo" : data ? "Read-only" : "Connecting"));
    $("snapshot-label").textContent = data ? `Snapshot ${time(data.generated_at)}` : "Awaiting first snapshot";
    $("snapshot-label").title = fullTime(data?.generated_at);
    $("poll-label").textContent = state.paused ? "Automatic refresh paused · snapshot retained" : `Read-only · refresh every ${refreshSeconds()}s`;
    $("poll-toggle").textContent = state.paused ? "Resume" : "Pause";
    $("poll-toggle").setAttribute("aria-pressed", String(state.paused));
    $("poll-toggle").title = `${state.paused ? "Resume" : "Pause"} automatic refresh`;
    $("refresh-button").disabled = state.loading;
    $("info-refresh").textContent = `${refreshSeconds()} seconds${state.paused ? " (paused)" : ""}`;
    $("info-mode").textContent = data ? `${mode || titleCase(data.mode)} · read-only` : "Unavailable";
    $("info-readiness").textContent = readiness ? `${readinessLabel} · ${state.error || state.paused ? "Current readiness unconfirmed" : string(readiness.message)}` : "Unavailable";
    $("nav-nodes").textContent = data ? count(data.summary?.nodes) : "—";
    $("nav-peers").textContent = data ? String(list(data.peers).length) : "—";
    $("nav-lsps").textContent = data ? String(list(data.lsps).length) : "—";
    $("nav-events").textContent = data ? String(list(data.events).length) : "—";
  }

  function renderStats() {
    const summary = state.data?.summary || {};
    $("stat-nodes").textContent = count(summary.nodes);
    $("stat-links").textContent = number(summary.directed_links) === null ? "No topology snapshot" : `${count(summary.directed_links)} directed TE links`;
    const observe = state.data?.operating_mode === "observe" && state.data?.mode !== "demo";
    $("stat-peers").textContent = observe ? "Disabled" : count(summary.pcep_peers);
    const peers = list(state.data?.peers).filter((peer) => peer.protocol === "PCEP");
    $("stat-peer-detail").textContent = observe ? "Intentionally disabled in observe mode" : state.error ? "Current session freshness unavailable" : peers.length ? `${peers.filter(fresh).length} fresh · ${peers.filter((peer) => peer.synchronized === true).length} synchronized` : "Session state unavailable";
    $("stat-lsps").textContent = state.error ? "—" : count(summary.active_lsps);
    $("stat-lsp-detail").textContent = state.error ? `Last snapshot: ${count(summary.active_lsps)} reported active` : state.data ? `${list(state.data.lsps).length} reported · forwarding unverified` : "Not forwarding verification";
    $("stat-computations").textContent = count(summary.computations);
    const latest = list(state.data?.computations)[0];
    $("stat-computation-detail").textContent = latest ? `Latest ${titleCase(latest.status)} · ${time(latest.timestamp)}` : "No computation outcomes";
  }

  function renderSources() {
    const sidebar = $("sidebar-sources"), cards = $("source-cards"), info = $("info-sources");
    sidebar.replaceChildren(); cards.replaceChildren(); info.replaceChildren();
    const sources = list(state.data?.sources);
    if (!sources.length) { sidebar.append(el("p", "muted", "No source snapshot")); cards.append(el("div", "card card-body muted", "No source health information available.")); return; }
    for (const item of sources) {
      const status = sourceState(item);
      const row = el("div", "sidebar-source");
      row.append(el("span", `dot ${dotClass(status)}`), el("span", "", string(item.label, item.id)), el("small", "", titleCase(status)));
      row.title = string(item.message, titleCase(status)); sidebar.append(row);
      const card = el("article", "card source-card"); card.append(el("h3", "", string(item.label, item.id)));
      const line = el("div"); line.append(pill(status), el("span", "source-age", status === "disabled" ? "Intentionally disabled" : age(sourceAge(item)))); card.append(line);
      card.append(el("p", "", string(item.message, "No source details reported.")));
      if (status !== "disabled") card.append(el("p", "mono", fullTime(item.timestamp)));
      cards.append(card);
      info.append(el("div", "", `${string(item.label, item.id)}: ${titleCase(status)}${status === "disabled" ? " · intentionally disabled in observe mode" : ` · ${age(sourceAge(item))}`}`));
    }
  }

  function routeFor(lsp) {
    const nodes = list(state.data?.topology?.nodes), links = list(state.data?.topology?.links);
    const metadata = list(lsp?.ero_hops);
    const hops = metadata.length ? metadata : list(lsp?.ero).map((address) => ({ address }));
    let current = nodes.find((node) => node.id === lsp?.source || node.loopback === lsp?.source);
    const result = { links: [], hops: [], unresolved: false };
    for (const raw of hops) {
      const hop = raw && typeof raw === "object" ? raw : {};
      const address = string(hop.address, "Unknown address");
      const exactHost = hop.loose === false && (hop.type === 1 && hop.prefix_length === 32 || hop.type === 2 && hop.prefix_length === 128);
      const prefix = typeof hop.prefix_length === "number" ? `/${hop.prefix_length}` : " · prefix unknown";
      const routeType = hop.loose === true ? "Loose" : hop.loose === false ? "Strict" : "Strictness unknown";
      const label = `${routeType}${prefix}`;
      const options = exactHost && current && sourceState(source("topology")) === "fresh" ? links.filter((link) => link.source === current.id && link.remote_address === address) : [];
      if (options.length !== 1) { result.hops.push({ address, node: "Unresolved hop", label }); result.unresolved = true; current = null; continue; }
      const link = options[0]; result.links.push(link);
      current = nodes.find((node) => node.id === link.target);
      result.hops.push({ address, node: string(current?.id, link.target), label });
    }
    return result;
  }

  function initialTopologyLayout(nodes, links) {
    const root = nodes.find((node) => /^PE1$/i.test(node.id)) || nodes.find((node) => /^PE/i.test(node.id)) || nodes[0];
    const levels = new Map([[root.id, 0]]), queue = [root.id];
    while (queue.length) {
      const id = queue.shift();
      for (const link of links) { if (link.source === id && !levels.has(link.target)) { levels.set(link.target, levels.get(id) + 1); queue.push(link.target); } }
    }
    let last = Math.max(...levels.values());
    for (const node of nodes) if (!levels.has(node.id)) levels.set(node.id, last + 1);
    last = Math.max(...levels.values());
    const columns = new Map();
    for (const node of nodes) { const level = levels.get(node.id); if (!columns.has(level)) columns.set(level, []); columns.get(level).push(node); }
    const points = new Map();
    for (const [level, group] of columns) {
      group.sort((a, b) => String(a.id).localeCompare(String(b.id)));
      group.forEach((node, i) => points.set(node.id, { x: last ? 65 + level * 520 / last : 325, y: group.length === 1 ? 144 : 65 + i * 158 / Math.max(1, group.length - 1) }));
    }
    return points;
  }

  const topologyLayouts = new Map();
  const topologyConnections = new Map();
  const lastTopologyScopes = new Map();
  const nodeIdentity = (node) => JSON.stringify([node.id, node.loopback || ""]);
  function rememberedNode(identity) {
    try {
      const value = JSON.parse(identity);
      if (Array.isArray(value) && value.length === 2 && value.every((part) => typeof part === "string" && part.length <= 512) && value[0]) return { id: value[0], loopback: value[1], area: "", retained: true };
    } catch { /* Ignore malformed historical identities. */ }
    return null;
  }
  function topologyScope(nodes) {
    const mode = state.data?.mode;
    if (!mode) return null;
    const pointer = `ninja-pce.topology-last.v1:${mode}`;
    if (!nodes.length && sourceState(source("topology")) !== "fresh") {
      let previous = lastTopologyScopes.get(mode);
      try { previous ||= localStorage.getItem(pointer); } catch { /* Storage is optional. */ }
      if (previous) {
        try {
          const value = JSON.parse(previous);
          if (Array.isArray(value) && value.length === 2 && value[0] === mode && typeof value[1] === "string" && value[1].length <= 512) return previous;
        } catch { /* Use the current scope when no valid previous scope exists. */ }
      }
    }
    const scope = JSON.stringify([mode, state.data.topology.name]);
    if (nodes.length && lastTopologyScopes.get(mode) !== scope) {
      lastTopologyScopes.set(mode, scope);
      try { localStorage.setItem(pointer, scope); } catch { /* The open page still remembers its topology. */ }
    }
    return scope;
  }
  function topologyLayout(nodes, links) {
    const ordered = nodes.filter((node) => node && rememberedNode(nodeIdentity(node))).sort((a, b) => String(a.id).localeCompare(String(b.id)));
    const scope = topologyScope(nodes);
    if (!scope) return { points: new Map(), nodes: [], links: [] };
    const storageKey = `ninja-pce.topology-layout.v1:${scope}`;
    if (!topologyLayouts.has(scope)) {
      let entries = [], connections = [];
      try {
        const saved = JSON.parse(localStorage.getItem(storageKey));
        if (saved?.version === 1 && Array.isArray(saved.points) && saved.points.length <= 512) {
          entries = saved.points.filter((entry) => Array.isArray(entry) && entry.length === 2 && typeof entry[0] === "string" && entry[0].length <= 8192 && rememberedNode(entry[0]) &&
            entry[1] && Number.isFinite(entry[1].x) && Number.isFinite(entry[1].y) && entry[1].x >= 65 && entry[1].x <= 585 && entry[1].y >= 45 && entry[1].y <= 223)
            .map(([identity, point]) => [nodeIdentity(rememberedNode(identity)), { x: point.x, y: point.y }]);
          if (Array.isArray(saved.links) && saved.links.length <= 4096) {
            connections = saved.links.filter((link) => link && ["id", "source", "target", "local_address", "remote_address"].every((key) => typeof link[key] === "string" && link[key].length <= 512))
              .map((link) => [link.id, { id: link.id, source: link.source, target: link.target, local_address: link.local_address, remote_address: link.remote_address, retained: true }]);
          }
        }
      } catch { /* Keep the map stable in memory when storage is unavailable. */ }
      topologyLayouts.set(scope, new Map(entries));
      topologyConnections.set(scope, new Map(connections));
    }
    const saved = topologyLayouts.get(scope), connections = topologyConnections.get(scope), identities = new Set(ordered.map(nodeIdentity));
    let changed = false;
    // Reused names with new addresses must not inherit former connection history.
    for (const identity of saved.keys()) {
      const previous = rememberedNode(identity);
      if (!previous) { saved.delete(identity); changed = true; continue; }
      if (ordered.some((node) => node.id === previous.id && nodeIdentity(node) !== identity)) {
        saved.delete(identity); changed = true;
        for (const [id, link] of connections) if (link.source === previous.id || link.target === previous.id) connections.delete(id);
      }
    }
    // Absent routers keep their slots so that a flap does not rearrange the map.
    const missing = ordered.filter((node) => !saved.has(nodeIdentity(node)));
    if (missing.length) {
      const seeded = saved.size > 0;
      const initial = initialTopologyLayout(ordered, links);
      const candidates = [];
      for (let row = 0; row < 3; row++) for (let column = 0; column < 9; column++) candidates.push({ x: 65 + column * 65, y: 65 + row * 79 });
      for (const node of missing) {
        const preferred = initial.get(node.id), occupied = [...saved.values()];
        // Preserve the initial automatic layout; newcomers use an unoccupied slot.
        const clearance = (point) => occupied.length ? Math.min(...occupied.map((other) => Math.hypot(point.x - other.x, point.y - other.y))) : Infinity;
        let point = preferred;
        if (seeded && clearance(point) < 60) {
          point = [...candidates].sort((a, b) => {
            const aFree = clearance(a) >= 60, bFree = clearance(b) >= 60;
            if (aFree !== bFree) return aFree ? -1 : 1;
            return aFree ? Math.hypot(a.x - preferred.x, a.y - preferred.y) - Math.hypot(b.x - preferred.x, b.y - preferred.y) : clearance(b) - clearance(a);
          })[0];
        }
        saved.set(nodeIdentity(node), point);
      }
      for (const identity of saved.keys()) {
        if (saved.size <= 512) break;
        if (!identities.has(identity)) saved.delete(identity);
      }
      changed = true;
    }
    for (const link of links) {
      // History contains identity and geometry only, never bandwidth or metrics.
      const previous = connections.get(link.id);
      const record = { id: link.id, source: link.source, target: link.target, local_address: link.local_address || "", remote_address: link.remote_address || "", retained: true };
      if (!previous || Object.keys(record).some((key) => previous[key] !== record[key])) { connections.set(link.id, record); changed = true; }
    }
    const currentLinks = new Set(links.map((link) => link.id));
    for (const id of connections.keys()) {
      if (connections.size <= 4096) break;
      if (!currentLinks.has(id)) { connections.delete(id); changed = true; }
    }
    if (changed) {
      try { localStorage.setItem(storageKey, JSON.stringify({ version: 1, points: [...saved], links: [...connections.values()] })); } catch { /* In-memory history remains available. */ }
    }
    const displayed = new Map(ordered.map((node) => [node.id, node])), points = new Map();
    for (const identity of saved.keys()) {
      const node = rememberedNode(identity);
      if (node && !displayed.has(node.id)) displayed.set(node.id, node);
    }
    for (const node of displayed.values()) points.set(node.id, saved.get(nodeIdentity(node)));
    const retainedLinks = [...connections.values()].filter((link) => !currentLinks.has(link.id) && displayed.has(link.source) && displayed.has(link.target));
    return { points, nodes: [...displayed.values()], links: [...links, ...retainedLinks] };
  }

  function reservation(link) {
    const capacity = number(link.reservable_bw_bps), reserved = number(link.reserved_bw_bps);
    if (capacity === null || capacity === 0 || reserved === null) return null;
    return reserved / capacity * 100;
  }
  const reservationClass = (percent) => percent === null ? "unknown" : percent < 50 ? "green" : percent <= 80 ? "amber" : "red";
  const reservationText = (percent) => percent === null ? "Unknown" : `${percent.toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;

  function topologyCircuits(links, points) {
    const circuits = new Map(), pairs = new Map();
    for (const link of links) {
      if (!points.has(link.source) || !points.has(link.target)) continue;
      const endpoints = [[link.source, link.local_address || ""], [link.target, link.remote_address || ""]]
        .sort((a, b) => String(a[0]).localeCompare(String(b[0])));
      // Reciprocal interface addresses identify a circuit; node names alone
      // would collapse parallel links. Unnumbered/ambiguous links stay separate.
      const key = link.local_address && link.remote_address ? JSON.stringify(endpoints) : JSON.stringify(["unpaired", link.id]);
      if (!circuits.has(key)) circuits.set(key, []);
      circuits.get(key).push(link);
    }
    for (const [key, circuit] of circuits) {
      const nodeIds = [circuit[0].source, circuit[0].target].sort((a, b) => {
        const left = points.get(a), right = points.get(b);
        return left.x - right.x || left.y - right.y || String(a).localeCompare(String(b));
      });
      const forward = circuit.filter((link) => link.source === nodeIds[0]), reverse = circuit.filter((link) => link.source === nodeIds[1]);
      const entries = forward.length <= 1 && reverse.length <= 1 ? [{ key, forward: forward[0], reverse: reverse[0] }] :
        circuit.map((link) => ({ key: `${key}:${link.id}`, forward: link.source === nodeIds[0] ? link : undefined, reverse: link.source === nodeIds[1] ? link : undefined }));
      const pairKey = JSON.stringify([...nodeIds].sort());
      if (!pairs.has(pairKey)) pairs.set(pairKey, []);
      for (const entry of entries) pairs.get(pairKey).push({ ...entry, source: nodeIds[0], target: nodeIds[1] });
    }
    const labels = [];
    for (const pair of pairs.values()) {
      pair.sort((a, b) => a.key.localeCompare(b.key));
      pair.forEach((entry, index) => {
        const a = points.get(entry.source), b = points.get(entry.target), dx = b.x - a.x, dy = b.y - a.y;
        const distance = Math.hypot(dx, dy) || 1, offset = (index - (pair.length - 1) / 2) * 18;
        labels.push({ ...entry, laneOffset: offset, x: (a.x + b.x) / 2 - dy / distance * offset, y: (a.y + b.y) / 2 + dx / distance * offset });
      });
    }
    return labels.sort((a, b) => a.key.localeCompare(b.key));
  }

  const topologyBounds = (x, y, width, height) => ({ left: x - width / 2, right: x + width / 2, top: y - height / 2, bottom: y + height / 2 });
  let topologyGeometryCache = null;
  function topologyGeometry(nodes, links, points) {
    const circuits = topologyCircuits(links, points);
    const currentCircuits = new Map(circuits.map((entry) => [entry.key, entry]));
    const currentLabels = (geometry) => ({ ...geometry, labels: geometry.labels.map((label) => ({ ...currentCircuits.get(label.key), ...label })) });
    const orderedNodes = [...nodes].sort((a, b) => String(a.id).localeCompare(String(b.id)));
    // Bandwidth, observation state and selected LSP never change the drawing.
    const key = JSON.stringify([orderedNodes.map((node) => [node.id, node.loopback, points.get(node.id)]),
      circuits.map((entry) => [entry.key, entry.source, entry.target, entry.forward?.id, entry.reverse?.id])]);
    if (topologyGeometryCache?.key === key) return currentLabels(topologyGeometryCache);
    const obstacles = orderedNodes.flatMap((node) => {
      const point = points.get(node.id), edge = /^PE/i.test(node.id), radius = edge ? 20 : 15;
      // Reserve the status line even while fresh, so observation changes do
      // not bend links differently or put a retained router's text over them.
      return [
        { node: node.id, icon: true, ...topologyBounds(point.x, point.y, radius * 2 + 8, radius * 2 + 8) },
        { node: node.id, ...topologyBounds(point.x, point.y + (edge ? 32 : 26), String(node.id).length * 6.5 + 8, 18) },
        { node: node.id, ...topologyBounds(point.x, point.y + (edge ? 47 : 41), string(node.loopback).length * 5.5 + 8, 16) },
        { node: node.id, ...topologyBounds(point.x, point.y + (edge ? 61 : 55), 134, 16) },
      ];
    });
    const paths = new Map(), labels = [], pairs = new Map();
    for (const entry of circuits) {
      const pairKey = JSON.stringify([entry.source, entry.target]);
      if (!pairs.has(pairKey)) pairs.set(pairKey, []);
      pairs.get(pairKey).push(entry);
    }
    const curvePoint = (path, t) => {
      const s = 1 - t, { start, end, control, second } = path;
      return second ? { x: s ** 3 * start.x + 3 * s * s * t * control.x + 3 * s * t * t * second.x + t ** 3 * end.x,
        y: s ** 3 * start.y + 3 * s * s * t * control.y + 3 * s * t * t * second.y + t ** 3 * end.y } :
        { x: s * s * start.x + 2 * s * t * control.x + t * t * end.x, y: s * s * start.y + 2 * s * t * control.y + t * t * end.y };
    };
    const curve = (link, control, second = null) => {
      const a = points.get(link.source), b = points.get(link.target);
      const toward = (point, gap) => {
        const dx = control.x - point.x, dy = control.y - point.y, distance = Math.hypot(dx, dy) || 1;
        return { x: point.x + dx / distance * gap, y: point.y + dy / distance * gap };
      };
      const start = toward(a, (/^PE/i.test(link.source) ? 20 : 15) + 4);
      const endControl = second || control, endDistance = Math.hypot(endControl.x - b.x, endControl.y - b.y) || 1;
      const gap = (/^PE/i.test(link.target) ? 20 : 15) + 7;
      const end = { x: b.x + (endControl.x - b.x) / endDistance * gap, y: b.y + (endControl.y - b.y) / endDistance * gap };
      const path = { link, start, end, control, second,
        d: `M${start.x},${start.y} ${second ? `C${control.x},${control.y} ${second.x},${second.y}` : `Q${control.x},${control.y}`} ${end.x},${end.y}` };
      const controls = [start, control, second || control, end];
      const length = controls.slice(1).reduce((sum, point, index) => sum + Math.hypot(point.x - controls[index].x, point.y - controls[index].y), 0);
      return { ...path, length, midpoint: curvePoint(path, .5) };
    };
    const collisions = (path) => {
      const { start, end, control, second, link } = path, margin = 6, controls = [start, control, second || control, end];
      const nearby = obstacles.filter((box) => !(box.icon && [link.source, link.target].includes(box.node)) &&
        box.right + margin >= Math.min(...controls.map((point) => point.x)) && box.left - margin <= Math.max(...controls.map((point) => point.x)) &&
        box.bottom + margin >= Math.min(...controls.map((point) => point.y)) && box.top - margin <= Math.max(...controls.map((point) => point.y)));
      const steps = Math.max(24, Math.min(120, Math.ceil(path.length / 4)));
      let hits = 0;
      for (let i = 0; i <= steps; i++) {
        const { x, y } = curvePoint(path, i / steps);
        if (x < 6 || x > 644 || y < 6 || y > 294) hits += 1;
        for (const box of nearby) if (x >= box.left - margin && x <= box.right + margin && y >= box.top - margin && y <= box.bottom + margin) hits += 1;
      }
      return hits;
    };
    for (const pair of pairs.values()) {
      const a = points.get(pair[0].source), b = points.get(pair[0].target), dx = b.x - a.x, dy = b.y - a.y;
      const distance = Math.hypot(dx, dy) || 1, normal = { x: -dy / distance, y: dx / distance };
      let chosen = null;
      const bends = [0, ...Array.from({ length: 10 }, (_, i) => [(i + 1) * 24, -(i + 1) * 24]).flat()];
      // Both directions and parallel circuits share one obstacle-free corridor.
      // A bounded least-obstructed fallback keeps unusually dense maps visible.
      for (const bend of bends) {
        const routes = pair.map((entry) => ({ entry, curves: [entry.forward, entry.reverse].filter(Boolean).map((link) => {
          const lane = entry.forward && entry.reverse ? link === entry.forward ? -9.5 : 9.5 : 0;
          const offset = bend + entry.laneOffset * 2 + lane;
          return curve(link, { x: (a.x + b.x) / 2 + normal.x * offset, y: (a.y + b.y) / 2 + normal.y * offset });
        }) }));
        const score = routes.reduce((sum, route) => sum + route.curves.reduce((value, path) => value + collisions(path), 0), 0);
        const cost = routes.reduce((sum, route) => sum + route.curves.reduce((value, path) => value + path.length, 0), 0);
        if (!chosen || score < chosen.score) chosen = { routes, score, cost };
        if (!score) break;
      }
      // Independent endpoint tangents can clear tightly stacked status text
      // when a single quadratic bend cannot approach a router safely.
      if (chosen.score) for (const bend of bends) {
        for (const [from, to, firstSide, secondSide] of [[-.15, 1.15, 1, 1], [0, 1, 1, 1], [1 / 3, 2 / 3, -1, 1], [1 / 3, 2 / 3, 1, -1]]) {
          const routes = pair.map((entry) => ({ entry, curves: [entry.forward, entry.reverse].filter(Boolean).map((link) => {
            const lane = entry.forward && entry.reverse ? link === entry.forward ? -9.5 : 9.5 : 0;
            const offset = entry.laneOffset * 2 + lane;
            const first = { x: a.x + dx * from + normal.x * (bend * firstSide + offset), y: a.y + dy * from + normal.y * (bend * firstSide + offset) };
            const second = { x: a.x + dx * to + normal.x * (bend * secondSide + offset), y: a.y + dy * to + normal.y * (bend * secondSide + offset) };
            return link.source === entry.source ? curve(link, first, second) : curve(link, second, first);
          }) }));
          const cost = routes.reduce((sum, route) => sum + route.curves.reduce((value, path) => value + path.length, 0), 0);
          if (!chosen.score && cost >= chosen.cost) continue;
          const score = routes.reduce((sum, route) => sum + route.curves.reduce((value, path) => value + collisions(path), 0), 0);
          if (score < chosen.score || score === chosen.score && cost < chosen.cost) chosen = { routes, score, cost };
        }
      }
      for (const { entry, curves } of chosen.routes) {
        for (const path of curves) paths.set(path.link.id, path.d);
        const reference = curves[0], before = curvePoint(reference, .49), after = curvePoint(reference, .51);
        const sign = reference.link.source === entry.source ? 1 : -1;
        const tx = (after.x - before.x) * sign, ty = (after.y - before.y) * sign, tangent = Math.hypot(tx, ty) || 1;
        labels.push({ key: entry.key, normal: { x: -ty / tangent, y: tx / tangent }, x: curves.reduce((sum, path) => sum + path.midpoint.x, 0) / curves.length,
          y: curves.reduce((sum, path) => sum + path.midpoint.y, 0) / curves.length });
      }
    }
    topologyGeometryCache = { key, paths, labels: labels.sort((a, b) => a.key.localeCompare(b.key)), obstacles };
    return currentLabels(topologyGeometryCache);
  }

  function renderTopology() {
    const graph = $("topology-svg"), observedNodes = list(state.data?.topology?.nodes), observedLinks = list(state.data?.topology?.links), lsp = selectedLsp();
    const display = topologyLayout(observedNodes, observedLinks), { points, nodes, links } = display;
    state.topologyDisplay = display;
    const topologyFresh = sourceState(source("topology")) === "fresh" && !state.paused;
    const observation = (item) => !topologyFresh ? "Observation unavailable" : item.retained ? "Not advertised" : "Currently advertised";
    graph.replaceChildren(); graph.toggleAttribute("hidden", !nodes.length); $("topology-empty").hidden = !!nodes.length;
    $("topology-name").textContent = string(state.data?.topology?.name, "Awaiting data");
    $("topology-freshness").textContent = titleCase(sourceState(source("topology")));
    if (!nodes.length) { $("topology-caption").textContent = "Topology is unavailable; no route is inferred."; renderInspector(); return; }
    const geometry = topologyGeometry(nodes, links, points), { paths } = geometry;
    const defs = svg("defs");
    const marker = svg("marker", { id: "link-arrow", viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 4, markerHeight: 4, orient: "auto" }); marker.append(svg("path", { d: "M 0 0 L 10 5 L 0 10 Z", class: "link-arrow" })); defs.append(marker); graph.append(defs);
    for (const link of links) {
      const unavailable = link.retained || !topologyFresh;
      const d = paths.get(link.id); if (!d) continue;
      const group = svg("g", { class: `link-group${unavailable ? " unavailable" : ""}${state.inspected?.kind === "link" && state.inspected.id === link.id ? " selected" : ""}`, tabindex: 0, role: "button", "aria-label": `${link.source} to ${link.target}, ${unavailable ? observation(link) : `${reservationText(reservation(link))} RSVP reserved`}`, "data-focus-key": `link-${link.id}` });
      group.append(svg("title", {}, unavailable ? `${link.source} → ${link.target} · ${observation(link)} · last observed connection; current bandwidth unknown` : `${link.source} → ${link.target}: ${reservationText(reservation(link))} reserved; ${bandwidth(link.available_bw_bps)} available; receiving ${string(link.remote_address)}`));
      group.append(svg("path", { d, class: `topology-link ${unavailable ? "unavailable" : reservationClass(reservation(link))}`, "marker-end": "url(#link-arrow)" }), svg("path", { d, class: "link-hit" }));
      const inspect = () => preserveFocus(() => { state.inspected = { kind: "link", id: link.id }; renderTopology(); });
      group.addEventListener("click", inspect); group.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); inspect(); } }); graph.append(group);
    }
    const route = routeFor(lsp), reported = string(lsp?.reported_state || lsp?.operational_state, "Unknown");
    const activeFresh = lsp && isActive(lsp) && fresh(lsp) && sourceState(source("topology")) === "fresh" && !state.paused;
    for (const link of route.links) if (paths.has(link.id)) graph.append(svg("path", { d: paths.get(link.id), class: `selected-path ${!fresh(lsp) || state.paused ? "stale" : isActive(lsp) ? activeFresh ? "flow" : "" : reported.toUpperCase() === "DOWN" ? "down" : "pending"}` }));
    const labelObstacles = [...geometry.obstacles];
    // Keep the shared label and its halo above both directional paths and ERO.
    for (const entry of geometry.labels) {
      const available = (link) => link && !link.retained && topologyFresh;
      if (!available(entry.forward) && !available(entry.reverse)) continue;
      const value = (link) => reservationText(available(link) ? reservation(link) : null);
      const detail = (link, source, target) => `${source} → ${target}${link ? ` (${string(link.local_address)} → ${string(link.remote_address)})` : ""}: ${value(link)} RSVP reserved${link && !available(link) ? ` · ${observation(link)}` : !link ? " · Not advertised" : ""}`;
      const description = `${detail(entry.forward, entry.source, entry.target)}; ${detail(entry.reverse, entry.target, entry.source)}`;
      const label = svg("g", { class: "link-label-group", role: "img", "aria-label": description });
      const text = svg("text", { x: entry.x, y: entry.y, class: "link-label", "aria-hidden": "true" }, `${value(entry.forward)} / ${value(entry.reverse)}`);
      label.append(svg("title", {}, description), text);
      graph.append(label);
      const width = (text.getComputedTextLength() || text.textContent.length * 5.5) + 8;
      // A transit node can occupy a long link's midpoint. Move only the label
      // along the link normal, with deterministic spacing and direction order.
      for (const offset of [0, 18, -18, 36, -36, 54, -54, 72, -72, 90, -90]) {
        const x = entry.x + entry.normal.x * offset, y = entry.y + entry.normal.y * offset, box = topologyBounds(x, y, width, 17);
        if (box.left < 4 || box.right > 646 || box.top < 4 || box.bottom > 296 ||
            labelObstacles.some((other) => box.left < other.right && box.right > other.left && box.top < other.bottom && box.bottom > other.top)) continue;
        text.setAttribute("x", x); text.setAttribute("y", y); labelObstacles.push(box); break;
      }
    }
    for (const node of nodes) {
      const unavailable = node.retained || !topologyFresh;
      const point = points.get(node.id), edge = /^PE/i.test(node.id), group = svg("g", { class: `node-group${unavailable ? " unavailable" : ""}${state.inspected?.kind === "node" && state.inspected.id === node.id ? " selected" : ""}`, tabindex: 0, role: "button", "aria-label": `${node.id}, ${string(node.loopback, "address unknown")}${unavailable ? `, ${observation(node)}` : ""}`, "data-focus-key": `node-${node.id}` });
      group.append(svg("title", {}, `${node.id} · ${string(node.loopback)} · ${observation(node)} · area ${string(node.area)}`), svg("circle", { cx: point.x, cy: point.y, r: edge ? 20 : 15, class: `topology-node${edge ? " edge" : ""}` }));
      group.append(svg("path", { d: `M${point.x - 7},${point.y - 3}h14 M${point.x - 4},${point.y - 6}l-3,3 3,3 M${point.x + 4},${point.y}l3,3 -3,3 M${point.x - 7},${point.y + 3}h14`, class: "node-symbol" }));
      group.append(svg("text", { x: point.x, y: point.y + (edge ? 36 : 30), class: "node-label" }, node.id), svg("text", { x: point.x, y: point.y + (edge ? 50 : 44), class: "node-address" }, string(node.loopback)));
      if (unavailable) group.append(svg("text", { x: point.x, y: point.y + (edge ? 64 : 58), class: "node-status" }, observation(node)));
      const inspect = () => preserveFocus(() => { state.inspected = { kind: "node", id: node.id }; renderTopology(); });
      group.addEventListener("click", inspect); group.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); inspect(); } }); graph.append(group);
    }
    $("topology-caption").textContent = !lsp ? "Select an LSP to inspect its reported ERO. Select a node or directed link for details." : !route.hops.length ? "Selected LSP has no explicit route; no path is inferred." : route.unresolved ? "Reported ERO is partly unresolved or ambiguous in this topology. Only exact directed matches are drawn." : `${isActive(lsp) ? "Reported" : "Intended"} ERO · ${string(lsp.name, lsp.id)}${!fresh(lsp) || state.paused ? " · retained snapshot" : ""}. Forwarding is not verified here.`;
    const retained = nodes.filter((node) => node.retained).length;
    if (!topologyFresh) $("topology-caption").textContent += " Last observed map; current topology observation is unavailable.";
    else if (retained || links.some((link) => link.retained)) $("topology-caption").textContent += ` ${retained ? `${retained} retained router${retained === 1 ? "" : "s"}; ` : ""}dashed outlines are not currently advertised.`;
    renderInspector();
  }

  function renderInspector() {
    const container = $("topology-inspector"); container.replaceChildren(); container.hidden = !state.inspected;
    if (!state.inspected) return;
    const item = state.inspected.kind === "node" ? list(state.topologyDisplay?.nodes).find((node) => node.id === state.inspected.id) : list(state.topologyDisplay?.links).find((link) => link.id === state.inspected.id);
    if (!item) { container.hidden = true; return; }
    const status = sourceState(source("topology")) !== "fresh" || state.paused ? "Observation unavailable" : item.retained ? "Not advertised" : "Currently advertised";
    const description = state.inspected.kind === "node" ? `${item.id} · ${string(item.loopback)} · ${status} · area ${string(item.area)}` : item.retained || status === "Observation unavailable" ? `${item.source} → ${item.target} · ${status} · last observed connection; current bandwidth unknown` : `${item.source} → ${item.target} · ${bandwidth(item.reserved_bw_bps)} reserved / ${bandwidth(item.reservable_bw_bps)} reservable · ${bandwidth(item.available_bw_bps)} available · metric ${string(item.metric)}`;
    container.append(el("span", "mono", description));
    const close = el("button", "button", "Close"); close.dataset.focusKey = "inspector-close";
    close.addEventListener("click", () => { const key = `${state.inspected.kind}-${state.inspected.id}`; state.inspected = null; renderTopology(); Array.from(document.querySelectorAll("[data-focus-key]")).find((node) => node.dataset.focusKey === key)?.focus({ preventScroll: true }); }); container.append(close);
  }

  function renderDetails() {
    const container = $("lsp-details"), lsp = selectedLsp(); container.replaceChildren();
    $("selected-lsp-label").textContent = lsp ? `Tunnel ${string(lsp.tunnel_id)}` : "None selected";
    if (!lsp) { const empty = el("div", "empty-state detail-empty"); empty.append(el("strong", "", "Select an LSP"), el("p", "", "Inspect reported state, bandwidth and explicit route.")); container.append(empty); return; }
    container.append(el("div", "detail-name", string(lsp.name, lsp.id)), el("div", "detail-subtitle mono", `PCC ${string(lsp.peer)} · tunnel ${string(lsp.tunnel_id)}`));
    const kv = el("dl", "kv");
    const rows = [["Reported state", pill(lsp.reported_state || lsp.operational_state)], ["Freshness", pill(fresh(lsp) ? "Fresh" : "Stale / unknown", fresh(lsp) ? "fresh" : "stale")], ["Head end", lsp.source], ["Tail end", lsp.destination], ["Reported bandwidth", bandwidth(lsp.bandwidth_bps)], ["Setup / hold priority", `${string(lsp.setup_priority)} / ${string(lsp.hold_priority)}`], ["PLSP-ID", lsp.plsp_id], ["Delegation reported", lsp.delegated === true ? "Yes" : lsp.delegated === false ? "No" : "Unknown"]];
    for (const [label, value] of rows) { kv.append(el("dt", "", label)); const dd = el("dd", value instanceof Node ? "" : "mono"); if (value instanceof Node) dd.append(value); else dd.textContent = string(value); kv.append(dd); } container.append(kv);
    if (state.paused) container.append(el("p", "detail-note warning", "Polling is paused. Freshness describes the retained snapshot, not a new observation."));
    else if (!fresh(lsp)) container.append(el("p", "detail-note warning", "This report is stale or its freshness is unknown. The state shown is historical."));
    const route = routeFor(lsp), heading = el("div", "route-heading"); heading.append(el("span", "", "Reported explicit route"), el("span", "mono", `${route.hops.length} hops`)); container.append(heading);
    if (!route.hops.length) container.append(el("p", "detail-note", "No ERO reported."));
    else { const timeline = el("ol", "route-list"); for (const hop of route.hops) { const row = el("li"), label = el("span", "", hop.node); label.append(el("span", "cell-secondary", hop.label)); row.append(label, el("span", "mono", string(hop.address))); timeline.append(row); } container.append(timeline); }
    if (route.unresolved) container.append(el("p", "detail-note warning", "Some hops cannot be matched uniquely to directed links."));
    if (list(lsp.rro).length) container.append(el("p", "detail-note mono", `Recorded route: ${lsp.rro.join(" → ")}`));
    const actions = el("div", "detail-actions");
    const view = el("button", "button primary compact", "View route"); view.dataset.focusKey = "details-view"; view.addEventListener("click", () => { navigate("topology"); $("topology-svg").scrollIntoView({ block: "nearest" }); });
    const copy = el("button", "button compact", "Copy ERO"); copy.dataset.focusKey = "details-copy"; copy.disabled = !list(lsp.ero).length;
    copy.addEventListener("click", async () => { try { await navigator.clipboard.writeText(lsp.ero.join("\n")); copy.textContent = "Copied"; } catch { copy.textContent = "Copy unavailable"; copy.title = "Clipboard is unavailable. Select addresses from the route list to copy."; } });
    actions.append(view, copy); container.append(actions, el("p", "detail-note", "Observation only. Delegation and ACTIVE reports do not establish durable ownership or forwarding proof."));
  }

  function renderLsps() {
    const body = $("lsp-table-body"); body.replaceChildren();
    const lsps = list(state.data?.lsps), filtered = lsps.filter((lsp) => {
      const active = isActive(lsp) && fresh(lsp);
      return (state.lspFilter === "all" || state.lspFilter === "active" && active || state.lspFilter === "issues" && !active) && [lsp.id, lsp.name, lsp.peer, lsp.source, lsp.destination, lsp.plsp_id, lsp.tunnel_id].some((value) => string(value, "").toLowerCase().includes(state.search));
    });
    $("lsp-count").textContent = `${filtered.length} of ${lsps.length} reported`;
    if (!filtered.length) emptyTable(body, 7, lsps.length ? "No LSPs match these filters." : "No LSP reports available.");
    for (const lsp of filtered) {
      const row = el("tr", String(lsp.id) === state.selected ? "selected" : ""); row.tabIndex = 0; row.dataset.lspId = String(lsp.id); row.dataset.focusKey = `lsp-${lsp.id}`; row.setAttribute("aria-selected", String(String(lsp.id) === state.selected)); row.setAttribute("aria-label", `Select ${string(lsp.name, lsp.id)}`);
      const name = el("span", "lsp-name", string(lsp.name, lsp.id)); name.append(el("span", "cell-secondary mono", `PCC ${string(lsp.peer)}`)); cell(row, name);
      cell(row, pill(lsp.reported_state || lsp.operational_state)); cell(row, pill(fresh(lsp) ? "Fresh" : "Stale / unknown", fresh(lsp) ? "fresh" : "stale"));
      cell(row, `${string(lsp.source)} → ${string(lsp.destination)}`, "mono nowrap"); cell(row, bandwidth(lsp.bandwidth_bps), "mono nowrap"); cell(row, String(list(lsp.ero).length), "mono"); cell(row, lsp.plsp_id, "mono");
      row.addEventListener("click", () => selectLsp(lsp.id)); row.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); selectLsp(lsp.id); } }); body.append(row);
    }
  }

  function renderLinks() {
    const body = $("link-table-body"), links = list(state.data?.topology?.links); body.replaceChildren(); $("link-count").textContent = `${links.length} directed links`;
    if (!links.length) emptyTable(body, 7, "No directed links available.");
    for (const link of links) { const row = el("tr"); cell(row, `${link.source} → ${link.target}`, "mono nowrap"); cell(row, link.remote_address, "mono"); cell(row, link.metric, "mono"); cell(row, bandwidth(link.reservable_bw_bps), "mono nowrap"); cell(row, bandwidth(link.reserved_bw_bps), "mono nowrap"); cell(row, bandwidth(link.available_bw_bps), "mono nowrap"); cell(row, reservationText(reservation(link)), "mono"); body.append(row); }
  }

  function renderPeers() {
    const body = $("peer-table-body"), peers = list(state.data?.peers); body.replaceChildren();
    if (!peers.length) emptyTable(body, 7, "No protocol peers reported.");
    for (const peer of peers) { const row = el("tr"); cell(row, peer.address, "mono"); cell(row, peer.protocol); cell(row, pill(peer.state)); cell(row, pill(fresh(peer) ? "Fresh" : "Stale / unknown", fresh(peer) ? "fresh" : "stale")); cell(row, peer.synchronized === true ? "Complete" : peer.synchronized === false ? "Incomplete" : "Not reported"); cell(row, list(peer.capabilities).join(", ") || "Not reported", "mono"); cell(row, number(peer.reports) === null ? "Unknown" : count(peer.reports), "mono"); body.append(row); }
    const ledger = state.data?.reservations, container = $("ledger-body"); container.replaceChildren(); $("ledger-status").textContent = ledger?.configured ? titleCase(ledger.state) : "Unconfigured";
    if (!ledger?.configured) { container.append(el("p", "muted", "No local reservation ledger is configured. Live PCEP reports are not automatically credited to this ledger.")); return; }
    container.append(el("p", "", `${count(ledger.holds)} local holds · ${list(ledger.entries).length} entries`), el("p", "detail-note", "Local holds are distinct from aggregate advertised RSVP reservations and reported LSP bandwidth."));
    for (const entry of list(ledger.entries)) container.append(el("p", "detail-note mono", `${string(entry.id)} · ${titleCase(entry.status)} · ${bandwidth(entry.bandwidth_bps)} · ${list(entry.edges).length} edges`));
  }

  function renderComputations() {
    const body = $("computation-table-body"), computations = list(state.data?.computations); body.replaceChildren();
    if (!computations.length) emptyTable(body, 6, "No computation outcomes available.");
    for (const item of computations) { const row = el("tr"); const timestamp = cell(row, time(item.timestamp), "mono"); timestamp.title = fullTime(item.timestamp); cell(row, item.peer, "mono"); cell(row, pill(item.status)); const path = list(item.path_nodes).join(" → ") || list(item.ero).join(" → ") || string(item.reason, "No path reported"); const route = cell(row, path, "mono"); if (item.reason && list(item.path_nodes).length) route.append(el("span", "cell-secondary", item.reason)); cell(row, bandwidth(item.bandwidth_bps), "mono nowrap"); cell(row, item.acknowledged === true ? `Acknowledged · ${titleCase(item.reported_state)}` : item.acknowledged === false ? "Not acknowledged" : "Not applicable"); body.append(row); }
  }

  function renderEvents() {
    const container = $("event-log"), scroll = container.scrollTop, atBottom = container.scrollHeight - container.clientHeight - scroll < 35;
    container.replaceChildren();
    const events = list(state.data?.events).slice(-80), filtered = events.filter((item) => state.logFilter === "all" || item.level === state.logFilter);
    $("event-count").textContent = `${filtered.length} events · latest 80 retained`;
    if (!filtered.length) container.append(el("p", "log-empty", events.length ? "No events match this filter." : "No controller events available."));
    for (const item of filtered) { const row = el("div", "log-row"), timestamp = el("span", "log-time", time(item.timestamp)); timestamp.title = fullTime(item.timestamp); const level = ["warning", "error"].includes(item.level) ? item.level : "info"; const peer = el("span", "log-source", string(item.peer, item.direction || "PCE")); peer.title = string(item.peer, item.direction || "Controller"); row.append(timestamp, el("span", `log-level ${level}`, level === "warning" ? "Warn" : titleCase(level)), peer, el("span", "log-message", `${string(item.name, "Event")}${item.detail ? ` · ${item.detail}` : ""}`)); container.append(row); }
    container.scrollTop = atBottom ? container.scrollHeight : scroll;
  }

  function preserveFocus(callback) {
    const key = document.activeElement?.dataset.focusKey;
    callback();
    if (key) Array.from(document.querySelectorAll("[data-focus-key]")).find((node) => node.dataset.focusKey === key)?.focus({ preventScroll: true });
  }
  function renderSelection() { preserveFocus(() => { renderLsps(); renderTopology(); renderDetails(); }); }
  function render() { preserveFocus(() => { renderChrome(); renderStats(); renderSources(); renderTopology(); renderDetails(); renderLsps(); renderLinks(); renderPeers(); renderComputations(); renderEvents(); }); }
  function refreshSeconds() { const configured = number(state.data?.refresh_seconds); return configured !== null ? Math.max(1, Math.min(60, configured)) : 5; }
  function schedule() { clearTimeout(state.timer); if (!state.paused) state.timer = setTimeout(refresh, refreshSeconds() * 1000); }
  async function refresh() {
    if (state.loading) return;
    clearTimeout(state.timer); state.loading = true; renderChrome();
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch("/api/controller", { cache: "no-store", signal: controller.signal, headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`Controller API returned HTTP ${response.status}.`);
      const data = await response.json();
      if (!data || data.schema_version !== 1 || data.read_only !== true || !["live", "demo"].includes(data.mode) || !Array.isArray(data.sources) || !Array.isArray(data.lsps) || !data.topology || !Array.isArray(data.topology.nodes) || !Array.isArray(data.topology.links)) throw new Error("Controller returned an unsupported snapshot.");
      state.data = data; state.receivedAt = Date.now(); state.error = null;
      if (!list(data.lsps).some((lsp) => String(lsp.id) === state.selected)) state.selected = data.lsps.length ? String(data.lsps[0].id) : null;
    } catch (error) { state.error = error.name === "AbortError" ? "Controller request timed out." : error instanceof SyntaxError ? "Controller returned invalid JSON." : string(error.message, "Controller connection failed."); }
    finally { clearTimeout(timeout); state.loading = false; render(); schedule(); }
  }
  $("poll-toggle").addEventListener("click", () => { state.paused = !state.paused; render(); if (state.paused) clearTimeout(state.timer); else refresh(); });
  $("refresh-button").addEventListener("click", refresh);
  try { $("log-timezone").textContent = `Times shown in ${Intl.DateTimeFormat().resolvedOptions().timeZone || "your local timezone"}.`; } catch { /* Keep the local-time label. */ }
  refresh();
})();
