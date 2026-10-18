/* Local guided administration. Credentials and CSRF state stay in memory. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const state = {authenticated: false, bootstrap: false, csrf: null, revision: null,
    preview: null, generation: 0, dirty: false, busy: false, loaded: false};
  const reconciliationGuidance = " Run ninja-pce admin reconcile with this workspace directory from the local CLI before another activation.";
  const requestError = (message, status) => Object.assign(new Error(message), {status});

  function message(value, kind = "") {
    $("message").textContent = value;
    $("message").className = "message" + (kind ? " " + kind : "");
  }

  async function api(path, body) {
    const options = {credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"}};
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      if (state.csrf) options.headers["X-CSRF-Token"] = state.csrf;
      options.body = JSON.stringify(body);
    }
    let response;
    try { response = await fetch(path, options); }
    catch (_) { throw requestError("Local administration is unavailable. Check the admin server, then refresh status. No result has been confirmed.", 0); }
    let value;
    try { value = await response.json(); }
    catch (_) { throw requestError("The admin server returned an unreadable response. Refresh before attempting another change.", response.status); }
    if (!response.ok) {
      const detail = typeof value.message === "string" ? value.message :
        typeof value.error === "string" ? value.error : "The admin server rejected this request.";
      throw requestError(detail, response.status);
    }
    return value;
  }

  function controls() {
    $("setup-fields").disabled = state.busy || !state.loaded;
    $("preview-button").disabled = state.busy || !state.loaded;
    $("save-button").disabled = state.busy || !state.preview;
    $("activate-button").disabled = state.busy || !state.preview;
    $("runtime-start").disabled = state.busy || !state.revision || state.dirty;
    $("runtime-stop").disabled = state.busy || !state.revision;
    $("reload-config").disabled = state.busy;
    $("logout").disabled = state.busy;
  }

  function invalidate() {
    state.generation += 1;
    state.preview = null;
    state.dirty = true;
    $("preview-panel").hidden = true;
    $("preview-files").replaceChildren();
    $("preview-changes").replaceChildren();
    $("edit-state").textContent = "Unsaved edits · preview these values before saving.";
    controls();
  }

  function errorMessage(error) {
    if (error.status === 401 && state.authenticated) {
      showAuth(false);
      message("Your session ended. Sign in again to continue.", "error");
      return;
    }
    if (error.status === 409) {
      invalidate();
      message("The saved revision changed or this preview expired. Reload saved configuration, review your changes, then preview again. " + error.message, "error");
      return;
    }
    message(error.message, "error");
  }

  function showAuth(bootstrap) {
    state.authenticated = false;
    state.bootstrap = bootstrap;
    state.csrf = null;
    state.preview = null;
    state.loaded = false;
    state.generation += 1;
    $("workspace").hidden = true;
    $("session-actions").hidden = true;
    $("auth-panel").hidden = false;
    $("auth-heading").textContent = bootstrap ? "Create an administrator" : "Sign in";
    $("auth-description").textContent = bootstrap ? "Initialize this local administration account." : "Use your local administrator account.";
    $("auth-submit").textContent = bootstrap ? "Create administrator" : "Sign in";
    $("bootstrap-token-label").hidden = !bootstrap;
    $("bootstrap-token").required = bootstrap;
    $("bootstrap-hint").hidden = !bootstrap;
    $("auth-password").autocomplete = bootstrap ? "new-password" : "current-password";
    $("auth-password").value = "";
    $("bootstrap-token").value = "";
    controls();
  }

  function field(label, className, value, options = {}) {
    const wrapper = document.createElement("label");
    wrapper.append(document.createTextNode(label));
    const input = document.createElement(options.select ? "select" : "input");
    input.className = className;
    if (!options.select) {
      input.type = options.type || "text";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.value = value ?? "";
      input.required = true;
      for (const key of ["min", "max", "step", "maxLength", "readOnly", "placeholder"]) {
        if (options[key] !== undefined) input[key] = options[key];
      }
    }
    wrapper.append(input);
    if (options.hint) {
      const hint = document.createElement("small");
      hint.textContent = options.hint;
      wrapper.append(hint);
    }
    return wrapper;
  }

  function removeButton(row, kind) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button remove";
    button.textContent = "Remove";
    button.setAttribute("aria-label", "Remove " + kind);
    button.addEventListener("click", () => { row.remove(); updateRows(); invalidate(); });
    return button;
  }

  function addPeer(value = {}) {
    const row = document.createElement("div");
    row.className = "row peer-row";
    row.append(field("Peer address", "peer-address", value.address, {placeholder: "192.0.2.20"}),
      field("Peer ASN", "peer-asn", value.asn, {type: "number", min: 1, max: 4294967295, step: 1}));
    row.querySelector(".peer-address").setAttribute("aria-describedby", "source-management-hint");
    row.append(removeButton(row, "BGP-LS peer"));
    $("peer-rows").append(row);
  }

  function addNode(value = {}) {
    const row = document.createElement("div");
    row.className = "row node-row";
    row.append(field("TE router ID", "node-router-id", value.router_id, {placeholder: "192.0.2.1"}),
      field("Node name", "node-name", value.name, {maxLength: 128, placeholder: "ABR1"}));
    row.append(removeButton(row, "node label"));
    $("node-rows").append(row);
  }

  function addAbr(value = {}) {
    const row = document.createElement("div");
    row.className = "row abr-row";
    const select = field("Labeled node", "abr-node", "", {select: true});
    select.querySelector("select").dataset.selected = value.node_id || "";
    row.append(select, field("TE router ID", "abr-te-router-id", value.te_router_id, {readOnly: true}),
      field("Advertised IGP router ID", "abr-igp-router-id", value.igp_router_id, {placeholder: "192.0.2.1"}),
      field("Area memberships", "abr-areas", (value.areas || []).join(", "),
        {placeholder: "0.0.0.0, 0.0.0.17", hint: "Comma-separated area IDs; list every explicit membership."}));
    row.append(removeButton(row, "ABR binding"));
    row.querySelector(".abr-node").addEventListener("change", () => syncAbr(row));
    $("abr-rows").append(row);
  }

  function nodes() {
    return [...$("node-rows").children].map(row => ({
      router_id: row.querySelector(".node-router-id").value.trim(),
      name: row.querySelector(".node-name").value.trim()
    }));
  }

  function syncAbr(row) {
    const node = nodes().find(item => item.name === row.querySelector(".abr-node").value);
    row.querySelector(".abr-te-router-id").value = node ? node.router_id : "";
  }

  function updateRows() {
    $("nodes-empty").hidden = $("node-rows").children.length > 0;
    $("abrs-empty").hidden = $("abr-rows").children.length > 0;
    $("add-abr").disabled = $("node-rows").children.length === 0;
    const knownNodes = nodes();
    for (const row of $("abr-rows").children) {
      const select = row.querySelector(".abr-node");
      const selected = select.value || select.dataset.selected || "";
      select.replaceChildren();
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Choose an explicit node label";
      select.append(placeholder);
      for (const node of knownNodes.filter(item => item.name && item.router_id)) {
        const option = document.createElement("option");
        option.value = node.name;
        option.textContent = node.name + " · " + node.router_id;
        select.append(option);
      }
      select.required = true;
      select.value = selected;
      select.dataset.selected = "";
      syncAbr(row);
    }
  }

  const inputs = {
    "controller-name": ["controller", "name"], "controller-router-id": ["controller", "router_id"],
    "controller-local-as": ["controller", "local_as"], "controller-source-address": ["controller", "source_address"],
    "controller-bgp-port": ["controller", "bgp_port"], "bgpls-interval": ["bgpls", "interval_seconds"],
    "bgpls-timeout": ["bgpls", "timeout_seconds"], "bgpls-max-age": ["bgpls", "max_age_seconds"],
    "domain-protocol": ["routing_domain", "protocol_id"], "domain-instance": ["routing_domain", "instance_id"],
    "domain-as": ["routing_domain", "autonomous_system"], "domain-bgpls-id": ["routing_domain", "bgpls_identifier"]
  };

  function fill(setup) {
    if (!Number.isSafeInteger(setup.routing_domain.instance_id)) {
      state.loaded = false;
      throw requestError("This revision has an instance ID beyond the exact integer range of guided setup. Use the local configuration workflow to retain that identity; this form cannot safely edit it.", 0);
    }
    for (const [id, [section, key]] of Object.entries(inputs)) $(id).value = setup[section][key];
    $("peer-rows").replaceChildren();
    $("node-rows").replaceChildren();
    $("abr-rows").replaceChildren();
    setup.bgpls.peers.forEach(addPeer);
    setup.nodes.forEach(addNode);
    setup.abr_bindings.forEach(addAbr);
    updateRows();
  }

  function typedSetup() {
    const setup = {version: 1, operating_mode: "observe", controller: {},
      bgpls: {}, routing_domain: {}, nodes: nodes(), abr_bindings: []};
    for (const [id, [section, key]] of Object.entries(inputs)) {
      const input = $(id);
      const numeric = input.type === "number" || input.tagName === "SELECT";
      const value = numeric ? Number(input.value) : input.value.trim();
      if (numeric && (section !== "bgpls") && !Number.isSafeInteger(value)) {
        throw requestError("Use an exact whole number within the guided setup range for " + key.replaceAll("_", " ") + ".", 0);
      }
      setup[section][key] = value;
    }
    setup.bgpls.peers = [...$("peer-rows").children].map(row => ({
      address: row.querySelector(".peer-address").value.trim(), asn: Number(row.querySelector(".peer-asn").value)
    }));
    setup.abr_bindings = [...$("abr-rows").children].map(row => ({
      node_id: row.querySelector(".abr-node").value,
      te_router_id: row.querySelector(".abr-te-router-id").value,
      igp_router_id: row.querySelector(".abr-igp-router-id").value.trim(),
      areas: row.querySelector(".abr-areas").value.split(",").map(value => value.trim()).filter(Boolean)
    }));
    return setup;
  }

  function revision(value) {
    state.revision = value;
    $("saved-revision").textContent = value ? value.slice(0, 12) : "Not configured";
    $("saved-revision").title = value || "";
  }

  async function loadConfig() {
    const value = await api("/api/admin/config");
    revision(value.revision);
    const ownershipHints = {
      managed: "This setup owns GoBGP; save and activate applies the reviewed BGP configuration.",
      external: "GoBGP is managed externally. This setup applies collector settings; review generated GoBGP changes through the external service workflow."
    };
    $("source-management-hint").textContent = Object.hasOwn(ownershipHints, value.source_management) ? ownershipHints[value.source_management] : "Check source ownership before activation.";
    fill(value.setup);
    state.loaded = true;
    state.dirty = false;
    state.preview = null;
    state.generation += 1;
    $("preview-panel").hidden = true;
    $("preview-files").replaceChildren();
    $("edit-state").textContent = value.revision ? "Saved values loaded · preview any changes before saving." : "Documentation example values · replace them with your assigned identities.";
    $("configuration-status").textContent = value.revision ? "Saved on disk" : "Defaults only · nothing saved";
    controls();
  }

  function statusView(value) {
    const runtime = value.runtime || {};
    const rawStatus = typeof runtime.state === "string" ? runtime.state :
      typeof runtime.status === "string" ? runtime.status : runtime.running === true ? "RUNNING" : runtime.running === false ? "STOPPED" : "UNKNOWN";
    const status = rawStatus.toUpperCase();
    $("runtime-state").textContent = status.replaceAll("_", " ");
    $("runtime-state").className = /RUNNING|ACTIVE/.test(status) ? "good" : "";
    $("runtime-detail").textContent = runtime.message || (status === "UNKNOWN" ? "Runtime status has not been confirmed." : "Local observer process state; separate from topology readiness.");
    const readiness = value.readiness || {};
    $("readiness-state").textContent = typeof readiness.state === "string" ? readiness.state.replaceAll("_", " ") : "Unknown";
    $("readiness-state").className = readiness.ready === true ? "good" : "warn";
    $("readiness-detail").textContent = readiness.message || "No current network readiness has been confirmed.";
    if (value.transaction && typeof value.transaction.status === "string") {
      $("configuration-status").textContent = value.transaction.status.replaceAll("_", " ");
    }
  }

  async function refreshStatus() {
    if (!state.authenticated) return;
    try { statusView(await api("/api/admin/status")); }
    catch (error) {
      $("runtime-state").textContent = "Unavailable";
      $("runtime-state").className = "";
      $("runtime-detail").textContent = "Current process state is unconfirmed.";
      $("readiness-state").textContent = "Unavailable";
      $("readiness-state").className = "warn";
      $("readiness-detail").textContent = "Current network readiness is unconfirmed.";
      errorMessage(error);
    }
  }

  async function refreshAudit() {
    if (!state.authenticated) return;
    try {
      const value = await api("/api/admin/audit");
      $("audit-events").replaceChildren();
      for (const event of value.events || []) {
        const row = document.createElement("div");
        row.className = "audit-event";
        const time = document.createElement("time");
        const timestamp = event.timestamp || event.time || event.at;
        time.textContent = typeof timestamp === "number" ? new Date(timestamp * 1000).toLocaleString() : String(timestamp || "");
        row.append(time, document.createTextNode([event.action || event.event || "Administration", event.username || event.principal || event.actor,
          event.status || event.result, event.message].filter(Boolean).join(" · ")));
        $("audit-events").append(row);
      }
      if (!$("audit-events").children.length) {
        const empty = document.createElement("p");
        empty.className = "empty-note";
        empty.textContent = "No administration events recorded.";
        $("audit-events").append(empty);
      }
    } catch (error) { errorMessage(error); }
  }

  async function signedIn(value) {
    state.authenticated = true;
    state.csrf = value.csrf_token;
    $("auth-panel").hidden = true;
    $("workspace").hidden = false;
    $("session-actions").hidden = false;
    $("session-user").textContent = value.username || "Administrator";
    $("auth-password").value = "";
    $("bootstrap-token").value = "";
    state.loaded = false;
    controls();
    await loadConfig();
    await Promise.all([refreshStatus(), refreshAudit()]);
    if (state.authenticated) message("Review the observer settings below. No network readiness is implied by a saved revision.");
  }

  $("auth-form").addEventListener("submit", async event => {
    event.preventDefault();
    const button = $("auth-submit");
    button.disabled = true;
    const body = {username: $("auth-username").value.trim(), password: $("auth-password").value};
    if (state.bootstrap) body.token = $("bootstrap-token").value;
    try { await signedIn(await api(state.bootstrap ? "/api/admin/bootstrap" : "/api/admin/login", body)); }
    catch (error) { errorMessage(error); }
    finally {
      $("auth-password").value = "";
      $("bootstrap-token").value = "";
      button.disabled = false;
    }
  });

  $("logout").addEventListener("click", async () => {
    try {
      await api("/api/admin/logout", {});
      showAuth(false);
      message("Signed out.");
    } catch (error) { errorMessage(error); }
  });

  $("setup-form").addEventListener("input", event => {
    if (event.target.closest("#node-rows")) updateRows();
    invalidate();
  });
  $("setup-form").addEventListener("change", () => invalidate());
  $("add-peer").addEventListener("click", () => { addPeer(); invalidate(); });
  $("add-node").addEventListener("click", () => { addNode(); updateRows(); invalidate(); });
  $("add-abr").addEventListener("click", () => { addAbr(); updateRows(); invalidate(); });

  $("setup-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (state.busy || !state.loaded) return;
    if (!$("peer-rows").children.length) { message("Add at least one BGP-LS peer before previewing.", "error"); return; }
    state.preview = null;
    const generation = state.generation;
    state.busy = true;
    controls();
    try {
      const value = await api("/api/admin/preview", {expected_revision: state.revision, setup: typedSetup()});
      if (generation !== state.generation || !state.authenticated) return;
      if (value.write_authority !== false || !value.preview_id || value.expected_revision !== state.revision) {
        throw requestError("The server did not return an observation-only preview for this revision. Reload configuration and review again.", 0);
      }
      state.preview = value;
      $("preview-changes").replaceChildren();
      for (const change of value.changes || []) {
        const item = document.createElement("li");
        item.textContent = change;
        $("preview-changes").append(item);
      }
      $("preview-files").replaceChildren();
      for (const [filename, content] of Object.entries(value.files || {})) {
        const details = document.createElement("details");
        details.open = true;
        const heading = document.createElement("summary");
        heading.textContent = filename;
        const pre = document.createElement("pre");
        pre.textContent = content;
        pre.tabIndex = 0;
        details.append(heading, pre);
        $("preview-files").append(details);
      }
      $("preview-panel").hidden = false;
      $("edit-state").textContent = "Preview ready · saving applies these reviewed values.";
      message("Preview generated. Review all files before saving; the observer remains in observe mode.");
    } catch (error) { errorMessage(error); }
    finally { state.busy = false; controls(); }
  });

  async function apply(activate) {
    if (!state.preview || state.busy) return;
    const preview = state.preview;
    state.preview = null;
    state.busy = true;
    controls();
    message(activate ? "Saving and activating the reviewed observer revision…" : "Saving the reviewed configuration…");
    try {
      const value = await api("/api/admin/apply", {preview_id: preview.preview_id, expected_revision: preview.expected_revision, activate});
      await loadConfig();
      await Promise.all([refreshStatus(), refreshAudit()]);
      const descriptions = {SAVED: "Configuration saved. Activation has not been requested; network readiness is separate.",
        ACTIVE: "Observer revision activated. Check network readiness above for current topology availability.",
        ROLLED_BACK: "Activation failed and the previous revision was restored. Review runtime status and audit events.",
        FAILED: "The configuration transaction failed. Review runtime status and audit events before trying again.",
        UNCERTAIN: "Activation or rollback could not be confirmed."};
      const detail = value.message || descriptions[value.status] || "The transaction result is unrecognized. Refresh status and review audit events.";
      message(detail + (value.status === "UNCERTAIN" ? reconciliationGuidance : ""),
        ["SAVED", "ACTIVE"].includes(value.status) ? "success" : "error");
    } catch (error) { errorMessage(error); }
    finally { state.busy = false; controls(); }
  }
  $("save-button").addEventListener("click", () => apply(false));
  $("activate-button").addEventListener("click", () => apply(true));

  async function runtimeAction(action) {
    if (state.busy || !state.revision || action === "start" && state.dirty) return;
    state.busy = true;
    controls();
    try {
      const value = await api("/api/admin/runtime", {action, expected_revision: state.revision});
      statusView(value);
      await Promise.all([refreshStatus(), refreshAudit()]);
      const transaction = value.transaction || {};
      if (["FAILED", "UNCERTAIN"].includes(transaction.status)) {
        const detail = transaction.message || transaction.error || value.message ||
          (value.runtime || {}).message || "The runtime action could not be confirmed.";
        message("Observer " + action + " failed. " + detail +
          (transaction.status === "UNCERTAIN" ? reconciliationGuidance : ""), "error");
      } else {
        message(value.message || (action === "start" ? "Observer start requested. Check runtime and network readiness separately." : "Observer stop requested. Saved configuration is retained."));
      }
    } catch (error) { errorMessage(error); }
    finally { state.busy = false; controls(); }
  }
  $("runtime-start").addEventListener("click", () => runtimeAction("start"));
  $("runtime-stop").addEventListener("click", () => runtimeAction("stop"));
  $("reload-config").addEventListener("click", async () => {
    state.busy = true;
    controls();
    try { await loadConfig(); message("Saved configuration reloaded. Review and preview before saving changes."); }
    catch (error) { errorMessage(error); }
    finally { state.busy = false; controls(); }
  });
  $("refresh-status").addEventListener("click", refreshStatus);
  $("refresh-audit").addEventListener("click", refreshAudit);

  async function initialize() {
    try {
      const value = await api("/api/admin/session");
      if (value.authenticated) await signedIn(value);
      else {
        showAuth(value.bootstrap_required === true);
        message(value.bootstrap_required ? "Initialize the local administrator to begin guided setup." : "Sign in to view or change the local observer configuration.");
      }
    } catch (error) { message(error.message, "error"); }
  }
  initialize();
  window.setInterval(() => { if (state.authenticated && !state.busy) refreshStatus(); }, 10000);
})();
