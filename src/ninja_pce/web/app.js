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

  function navigate(view, updateHash = true) {
    state.view = Object.hasOwn(views, view) ? view : "overview";
    const [label, heading, description] = views[state.view];
    $("page-title").textContent = label;
    $("view-heading").textContent = heading;
    $("view-description").textContent = description;
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
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) && !$("info-dialog").open) {
      event.preventDefault();
      if (!["overview", "lsps"].includes(state.view)) navigate("lsps");
      $("lsp-search").focus();
    }
    if (event.key === "Escape" && document.activeElement === $("lsp-search")) { $("lsp-search").value = ""; state.search = ""; renderLsps(); }
  });
  $("info-toggle").addEventListener("click", () => $("info-dialog").showModal());
  $("info-close").addEventListener("click", () => $("info-dialog").close());

  function renderChrome() {
    const data = state.data;
    $("demo-banner").hidden = data?.mode !== "demo";
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
    $("info-mode").textContent = data ? `${titleCase(data.mode)} · read-only` : "Unavailable";
    $("nav-nodes").textContent = data ? count(data.summary?.nodes) : "—";
    $("nav-peers").textContent = data ? String(list(data.peers).length) : "—";
    $("nav-lsps").textContent = data ? String(list(data.lsps).length) : "—";
    $("nav-events").textContent = data ? String(list(data.events).length) : "—";
  }

  function renderStats() {
    const summary = state.data?.summary || {};
    $("stat-nodes").textContent = count(summary.nodes);
    $("stat-links").textContent = number(summary.directed_links) === null ? "No topology snapshot" : `${count(summary.directed_links)} directed TE links`;
    $("stat-peers").textContent = count(summary.pcep_peers);
    const peers = list(state.data?.peers).filter((peer) => peer.protocol === "PCEP");
    $("stat-peer-detail").textContent = state.error ? "Current session freshness unavailable" : peers.length ? `${peers.filter(fresh).length} fresh · ${peers.filter((peer) => peer.synchronized === true).length} synchronized` : "Session state unavailable";
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
      const line = el("div"); line.append(pill(status), el("span", "source-age", age(sourceAge(item)))); card.append(line);
      card.append(el("p", "", string(item.message, "No source details reported.")));
      const timestamp = el("p", "mono", fullTime(item.timestamp)); card.append(timestamp); cards.append(card);
      info.append(el("div", "", `${string(item.label, item.id)}: ${titleCase(status)} · ${age(sourceAge(item))}`));
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
      const options = exactHost && current ? links.filter((link) => link.source === current.id && link.remote_address === address) : [];
      if (options.length !== 1) { result.hops.push({ address, node: "Unresolved hop", label }); result.unresolved = true; current = null; continue; }
      const link = options[0]; result.links.push(link);
      current = nodes.find((node) => node.id === link.target);
      result.hops.push({ address, node: string(current?.id, link.target), label });
    }
    return result;
  }

  function topologyLayout(nodes, links) {
    const root = nodes.find((node) => node.id === selectedLsp()?.source || node.loopback === selectedLsp()?.source) || nodes.find((node) => /^PE1$/i.test(node.id)) || nodes[0];
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

  function reservation(link) {
    const capacity = number(link.reservable_bw_bps), reserved = number(link.reserved_bw_bps);
    if (capacity === null || capacity === 0 || reserved === null) return null;
    return reserved / capacity * 100;
  }
  const reservationClass = (percent) => percent === null ? "unknown" : percent < 50 ? "green" : percent <= 80 ? "amber" : "red";
  const reservationText = (percent) => percent === null ? "Unknown" : `${percent.toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;

  function renderTopology() {
    const graph = $("topology-svg"), nodes = list(state.data?.topology?.nodes), links = list(state.data?.topology?.links), lsp = selectedLsp();
    graph.replaceChildren(); graph.toggleAttribute("hidden", !nodes.length); $("topology-empty").hidden = !!nodes.length;
    $("topology-name").textContent = string(state.data?.topology?.name, "Awaiting data");
    $("topology-freshness").textContent = titleCase(sourceState(source("topology")));
    if (!nodes.length) { $("topology-caption").textContent = "Topology is unavailable; no route is inferred."; renderInspector(); return; }
    const points = topologyLayout(nodes, links), paths = new Map(), pairs = new Map();
    for (const link of links) { const key = [link.source, link.target].sort().join("\u0000"); if (!pairs.has(key)) pairs.set(key, []); pairs.get(key).push(link); }
    const defs = svg("defs");
    const marker = svg("marker", { id: "link-arrow", viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 4, markerHeight: 4, orient: "auto" }); marker.append(svg("path", { d: "M 0 0 L 10 5 L 0 10 Z", class: "link-arrow" })); defs.append(marker); graph.append(defs);
    for (const link of links) {
      const a = points.get(link.source), b = points.get(link.target); if (!a || !b) continue;
      const pair = pairs.get([link.source, link.target].sort().join("\u0000"));
      const index = pair.indexOf(link), offset = pair.length === 1 ? 0 : (index - (pair.length - 1) / 2) * 19;
      const direction = String(link.source).localeCompare(String(link.target)) <= 0 ? 1 : -1;
      const dx = b.x - a.x, dy = b.y - a.y, distance = Math.hypot(dx, dy) || 1;
      const mx = (a.x + b.x) / 2 - dy / distance * offset * direction, my = (a.y + b.y) / 2 + dx / distance * offset * direction;
      const start = { x: a.x + dx / distance * 22, y: a.y + dy / distance * 22 }, end = { x: b.x - dx / distance * 24, y: b.y - dy / distance * 24 };
      const d = `M${start.x},${start.y} Q${mx},${my} ${end.x},${end.y}`; paths.set(link.id, d);
      const group = svg("g", { class: `link-group${state.inspected?.kind === "link" && state.inspected.id === link.id ? " selected" : ""}`, tabindex: 0, role: "button", "aria-label": `${link.source} to ${link.target}, ${reservationText(reservation(link))} RSVP reserved`, "data-focus-key": `link-${link.id}` });
      group.append(svg("title", {}, `${link.source} → ${link.target}: ${reservationText(reservation(link))} reserved; ${bandwidth(link.available_bw_bps)} available; receiving ${string(link.remote_address)}`));
      group.append(svg("path", { d, class: `topology-link ${reservationClass(reservation(link))}`, "marker-end": "url(#link-arrow)" }), svg("path", { d, class: "link-hit" }));
      group.append(svg("text", { x: (start.x + 2 * mx + end.x) / 4, y: (start.y + 2 * my + end.y) / 4 - 7, class: "link-label" }, reservationText(reservation(link))));
      const inspect = () => preserveFocus(() => { state.inspected = { kind: "link", id: link.id }; renderTopology(); });
      group.addEventListener("click", inspect); group.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); inspect(); } }); graph.append(group);
    }
    const route = routeFor(lsp), reported = string(lsp?.reported_state || lsp?.operational_state, "Unknown");
    const activeFresh = lsp && isActive(lsp) && fresh(lsp) && sourceState(source("topology")) === "fresh" && !state.paused;
    for (const link of route.links) if (paths.has(link.id)) graph.append(svg("path", { d: paths.get(link.id), class: `selected-path ${!fresh(lsp) || state.paused ? "stale" : isActive(lsp) ? activeFresh ? "flow" : "" : reported.toUpperCase() === "DOWN" ? "down" : "pending"}` }));
    for (const node of nodes) {
      const point = points.get(node.id), edge = /^PE/i.test(node.id), group = svg("g", { class: `node-group${state.inspected?.kind === "node" && state.inspected.id === node.id ? " selected" : ""}`, tabindex: 0, role: "button", "aria-label": `${node.id}, ${string(node.loopback, "address unknown")}`, "data-focus-key": `node-${node.id}` });
      group.append(svg("title", {}, `${node.id} · ${string(node.loopback)} · area ${string(node.area)}`), svg("circle", { cx: point.x, cy: point.y, r: edge ? 20 : 15, class: `topology-node${edge ? " edge" : ""}` }));
      group.append(svg("path", { d: `M${point.x - 7},${point.y - 3}h14 M${point.x - 4},${point.y - 6}l-3,3 3,3 M${point.x + 4},${point.y}l3,3 -3,3 M${point.x - 7},${point.y + 3}h14`, class: "node-symbol" }));
      group.append(svg("text", { x: point.x, y: point.y + (edge ? 36 : 30), class: "node-label" }, node.id), svg("text", { x: point.x, y: point.y + (edge ? 50 : 44), class: "node-address" }, string(node.loopback)));
      const inspect = () => preserveFocus(() => { state.inspected = { kind: "node", id: node.id }; renderTopology(); });
      group.addEventListener("click", inspect); group.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); inspect(); } }); graph.append(group);
    }
    $("topology-caption").textContent = !lsp ? "Select an LSP to inspect its reported ERO. Select a node or directed link for details." : !route.hops.length ? "Selected LSP has no explicit route; no path is inferred." : route.unresolved ? "Reported ERO is partly unresolved or ambiguous in this topology. Only exact directed matches are drawn." : `${isActive(lsp) ? "Reported" : "Intended"} ERO · ${string(lsp.name, lsp.id)}${!fresh(lsp) || state.paused ? " · retained snapshot" : ""}. Forwarding is not verified here.`;
    renderInspector();
  }

  function renderInspector() {
    const container = $("topology-inspector"); container.replaceChildren(); container.hidden = !state.inspected;
    if (!state.inspected) return;
    const item = state.inspected.kind === "node" ? list(state.data?.topology?.nodes).find((node) => node.id === state.inspected.id) : list(state.data?.topology?.links).find((link) => link.id === state.inspected.id);
    if (!item) { container.hidden = true; return; }
    const description = state.inspected.kind === "node" ? `${item.id} · ${string(item.loopback)} · area ${string(item.area)}` : `${item.source} → ${item.target} · ${bandwidth(item.reserved_bw_bps)} reserved / ${bandwidth(item.reservable_bw_bps)} reservable · ${bandwidth(item.available_bw_bps)} available · metric ${string(item.metric)}`;
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
