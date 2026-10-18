# Structured diagnostics

PR 5 adds one read-only report format to the CLI, guided setup and dashboard.
Keep `doctor` as the fast static check. Diagnostics describe known observations,
their age, missing evidence and manual next steps. They do not change services,
configuration, router state, reservations or recovery journals.

## CLI

Inspect a controller configuration and its retained observations:

```bash
./ninja-pce diagnostics --config .ninja-pce/workspace/config/controller.json
./ninja-pce diagnostics --config /etc/ninja-pce/controller.json --format text
```

Inspect an existing guided-setup workspace under its owning OS account, including
while management is running:

```bash
./ninja-pce diagnostics --directory .ninja-pce/admin-workspace
```

The workspace reader checks private directories, generated revision bytes and
their inventory without creating or acquiring runtime ownership locks. It never
initializes an absent workspace or replaces a journal. A CLI report does not
establish authentication of a running HTTP interface.

Explicitly request local live checks with `--live`:

```bash
./ninja-pce diagnostics --directory .ninja-pce/admin-workspace --live
./ninja-pce diagnostics --config /etc/ninja-pce/controller.json --live \
  --gobgpd /opt/ninja-pce/releases/<RELEASE>/bin/gobgpd --format text
```

Supply only a trusted native path. When supplied, its parent release's provenance
and inventory are compared with bounded native SHA-256 hashes. Native binaries
are inspected without executing their version or configuration commands.
Matching local hashes prove local consistency only. The report always leaves
independent signature authentication unknown; use the
[offline verifier and separately supplied trust key](offline_bundles.md) for that.

JSON is the default output. Exit status is `0` only when the aggregate is `ok`;
blocked, warning or unknown reports return `1`. Invalid configuration produces a
structured blocker before RPC probes. Missing metadata remains unknown.

## GUI and API

After signing in to [guided setup](authenticated_setup.md), use **Refresh
snapshot** or **Run live checks** in Diagnostics. Findings include a
stable code, severity, cause, evidence and manual next step, with a severity
filter and expandable evidence. Snapshot refresh never implicitly requests live
RPCs. No diagnostic control repairs a service or kills a PID.

| Route | Scope and boundary |
|---|---|
| `GET /api/admin/diagnostics` | Authenticated administration snapshot |
| `POST /api/admin/diagnostics` with `{"live":true}` | Authenticated session, exact Host/Origin, current CSRF token; explicit live checks |
| `GET /api/diagnostics` | Dashboard snapshot; authenticated when served by administration, otherwise protected by the existing deployment proxy |

The admin POST accepts exactly one boolean `live`; it accepts no process,
service, command or repair selection. Live starts are serialized per server and
limited to one per five seconds. Busy or changing configuration produces an
unknown report immediately. Management health and session routes remain
available while a live check waits. A revision change discards the sampled
report instead of combining configurations.

The standalone dashboard shows snapshot findings on Overview and Peers. Its
updated Nginx template permits the diagnostic GET under the existing Basic
authentication and TLS boundary. Existing proxy deployments need their reviewed
template updated before this route is available. No live endpoint is added to
the standalone dashboard.

Both UIs display report time and coverage. They clear findings after connectivity
loss, pause, logout or configuration/runtime changes as applicable, reject stale
or unconfirmed timestamps and expire a displayed report after thirty seconds.
Browser clock disagreement also makes coverage unconfirmed. Rendering uses text
nodes, including evidence; a previous response cannot repopulate a signed-out
or disconnected view.

## Evidence and limits

| Area | Snapshot evidence | Explicit live additions |
|---|---|---|
| Host | OS/architecture/Python profile, bounded RAM/disk counters, configured path modes/access | Numeric local address assignment via `ip -j address show`; clock synchronization via `timedatectl` |
| BGP-LS | Validated capture age, known collector fault code, required session and End-of-Rib state, executable availability | Capped `ListPeer` and binary `ListPath` read-only RPCs to the configured loopback GoBGP API; optional native provenance/hash comparison |
| Topology | Existing strict domain/Node/Link validation, exclusions, explicit ABR policy digest and fresh graph | RPC output remains separate from the collector's consistent before/RIB/after snapshot |
| PCEP | Mode, local listener metadata, recorded PCC freshness, deadtimer, synchronization, local versus remote OPEN capabilities and delegation | No active PCC transport probes or PCEP messages |
| Runtime | Existing process identity/configuration records, kernel listener owners, allowlisted original startup codes | Fixed systemd properties for the three managed units, including restart result/count and boot enablement |
| Persistence/security | Immutable admin revision inventory, protected paths, wildcard listener warnings and the current admin request's authentication boundary | No permission writes, boot activation, signature trust discovery or proxy authentication certification |

Listener inspection uses Linux kernel metadata. An occupied port alone does not
prove a conflict; expected unit ownership requires matching cgroup evidence.
An original startup failure such as `GOBGP_PORT_IN_USE` remains a blocker with
bounded owner evidence. Namespace or permission limits produce unknown
ownership. A free port is only a snapshot, never a reservation.

Each RPC subprocess has a two-second deadline, a 2 MiB stdout cap, no stderr
collection and no disk spool. Local address/clock commands share a two-second
budget; service commands share a separate two-second budget and a 64 KiB cap.
Only the diagnostic process's own timed-out children are terminated. Hashing is
limited to 128 MiB per binary. Local filesystem latency still applies.

Reports use `schema_version: 1`, an ISO `generated_at`, `scope`, `operating_mode`,
`overall`, per-severity `summary`, `findings`, `limitations` and `complete`.
Each finding has `code`, `severity`, `category`, `message`, `remediation`,
`evidence` and `observed_at`. Report generation time is not the underlying
capture time: capture timestamps and age appear in evidence when known.
Severities are `blocker`, `warning`, `unknown`, `info` and `ok`, in that priority.
An unknown check makes `complete` false even when another finding blocks use.
At most 256 input findings are normalized; truncation adds an unknown finding.
Evidence is bounded by depth/count/string limits and excludes credential,
command, environment, raw payload and stdout/stderr fields. Raw logs and router
configuration are never copied into diagnostic messages.

`read_only` is always true. `control_authority_verified` and
`forwarding_verified` are always false. A successful RPC, valid graph, running
service, synchronized PCC or observed delegation grants no write authority and
does not establish forwarding. Router running/startup persistence requires
separate authorized evidence; this service never logs into routers.

## Manual investigation

| Finding | Next step |
|---|---|
| `GOBGP_PORT_IN_USE` | Compare the reported PID/cgroup with the intended unit; use its normal service controls after verifying identity |
| `BGPLS_RPC_UNAVAILABLE` | Inspect the managed GoBGP API and collector client; generic stale files alone never establish this cause |
| `BGPLS_SESSION_DOWN` | Review the required exporter neighbor, BGP-LS family and transport |
| `BGPLS_EOR_INCOMPLETE` | Wait for the explicitly missing End-of-Rib and inspect restart state; absent metadata remains unknown |
| `TOPOLOGY_NODE_LINK_EXCLUDED` / `ABR_PROJECTION_POLICY_INVALID` | Review retained binary advertisements, explicit domain identities and approved ABR bindings |
| `PCEP_SYNC_INCOMPLETE` / `PCEP_DEADTIMER_EXPIRED` | Review PCC reports, keepalives, timers and current listener observations |
| `SERVICE_FAILED` | Inspect the structured startup cause and systemd Result/restart evidence before a deliberate restart |
| `HOST_PATH_WRITABLE_BY_OTHERS` | Review the intended service account and restrict protected state write access |
| `ADMIN_RECONCILIATION_REQUIRED` / `ADMIN_RUNTIME_UNCERTAIN` | Follow local ownership reconciliation in the setup guide; preserve all operational journals |

Coverage is validated with synthetic captures, disposable local HTTP/browser
tests, capped subprocess failures, provenance tampering and offline installed
package checks. These tests do not qualify a fresh VM, real router reboot,
production service restart or forwarding behavior.
