# Local reconciliation and reservation ownership

The controller admits demands against a shared directional reservation ledger before any configuration operation. All demands controlling the same network must use the same persistent state directory. The CLI defaults to `build/state` (or `NINJA_PCE_STATE_DIR`); the daemon uses its configured `state_dir`. A different state directory creates an independent accounting domain and must never be used to control the same resources concurrently.

The directory lock covers loading the ledger, computing the path, reserving its bandwidth, writing pending state, device delivery, and verification. Separate demand and device locks preserve identity and ordering. The lock uses `flock` and coordinates cooperating processes on one local filesystem. This is not distributed consensus, does not coordinate other controllers or manual router configuration, and must not be advertised as safe across independent machines or unreliable shared filesystems.

## Admission and retained resources

Each entry binds a service to a head-end/platform/tunnel identity, one state-file owner, exact directional edge IDs, bandwidth in integer bit/s, a plan ID, and lifecycle status. A second state file claiming the same LSP identity is rejected. Reusing one state file for a different LSP identity is rejected too. Cisco signaling rounds upward to integer kbps: computation and the ledger cover the rounded amount, so a fractional-kbps request cannot overbook the router's actual reservation.

A preview creates a `PLANNED` reservation. This makes competing previews conservative: with 100 Mbps remaining, two 80 Mbps demands cannot both be admitted against that edge. Planning demand B can change demand A's available-bandwidth evidence and plan ID once; after accounting stabilizes, repeated inputs are suppressed. Preview makes no network connection.

Before device contact the ledger persists `PUSHING` and LSP state persists a pending operation. `VERIFIED` requires structured operational evidence from the southbound verifier and successful permanent commit confirmation where applicable. Successful CLI transport does not establish operational success. An uncertain write, failed commit, failed operational check, or process interruption retains its resources. Restart observes an unresolved operation with read-only verification; it does not retry the configuration blindly. Even matching UP evidence cannot establish whether an interrupted IOS XR/Junos confirmed commit was permanently confirmed or may still expire. Recovery therefore returns `recovery_required` / `APPLIED_UNVERIFIED`, preserves pending state and reservations, records an explicit manual commit-outcome note, and leaves daemon health degraded. It never advances `last_verified` from that observation. Permanent commit-outcome recovery requires manual review; no automatic commit confirmation is attempted during restart. Changed desired state cannot execute while an earlier delivery remains unresolved.

Rerouting retains the union of old and new directional edges. On an edge shared by both paths, the held amount is the greater reservation; lowering requested bandwidth does not prematurely free the old reservation. Other demands continue to account for every held edge. The delivery adapter currently refuses to merge configuration into pre-existing TE resources because safe replacement and ownership-aware rollback need platform qualification. No automatic router deletion or resource release is implemented. `ROLLED_BACK` records an observation but still retains capacity; `RELEASED` is rejected. Releasing abandoned previews, retired paths, or failed changes requires manual recovery with independently verified ownership, absence/withdrawal, and a matching TED snapshot. Preserve state and audit files for that review; deleting the ledger is not a supported release procedure.

## Observed TED reservations

A new timestamp by itself never clears pending bandwidth. A trusted collector may supply explicit owned observations:

```yaml
metadata:
  source: lab-collector
  timestamp: "2026-10-09T09:00:00+00:00"
  authoritative: true
  complete: true
  reservations:
    - lsp_id: PE1:iosxr:tunnel-te100
      owner: /absolute/path/to/build/state/.ninja-pce-demand-state.json
      source: lab-collector
      observed_at: "2026-10-09T08:59:59+00:00"
      status: VERIFIED
      edges: [PE1-P3, P3-P4, P4-PE3]
      bandwidth_bps: 50000000
```

Use emitted normalized edge IDs and the absolute owner path from the ledger. The observation must exactly match identity, owner, current path edges, actual signaling bandwidth and collector source. Its timezone-aware time must be after the persisted network attempt and no later than the snapshot. The snapshot and all explicit per-edge source/timestamp overrides must be authoritative, complete, from the same source, fresh and not in the future. The default confirmation age limit is 60 seconds. Reported aggregate reservations must already debit the observed amount. Across all owned observations, credited bandwidth on each edge cannot exceed either the aggregate reservation or the debit in priority-7 unreserved bandwidth; contradictory evidence fails closed.

Valid evidence substitutes the observed debit for the matching local pending amount. The ledger retains its ownership and held resources, and rechecks evidence for each supplied snapshot. Missing or mismatched future observations restore conservative local accounting. Old reroute edges remain held. While recomputing the owning LSP, its explicitly confirmed observed debit is reusable; other demands still see it reserved. This model favors safe under-admission over guessing that unrelated aggregate changes belong to Ninja-PCE. A real topology collector and authenticated provenance are future work; a user-editable file is not independent router proof.

## Durable state and recovery

LSP state, shared reservations, and daemon retry state use schema version 2 with a SHA-256 checksum over canonical JSON excluding the `checksum` field. Writes use a unique same-directory temporary file, flush and `fsync`, atomic replacement, then directory `fsync`. A crash between ledger and LSP writes can retain unused capacity, but cannot erase admitted capacity. The checksum detects accidental corruption; it is not an authentication mechanism. Schema validation rejects invalid resource values even with a correct checksum. Missing or corrupt ledgers fail closed when reservation-owning LSP state still exists.

Version-1 LSP state can be loaded for preview and rewritten with `migrated_from: 1`. Old `last_applied` configuration does not provide reservation ownership or verified operational state. Execution of migrated applied state requires manual recovery. No evidence is fabricated during migration. Unknown schema versions and checksum mismatches fail closed. Recover from a consistent trusted backup and reobserve the network before resuming; do not repair corruption by clearing resource holds.

The LSP file separates `desired`, `pending`, `observed`, and `last_verified`. The last verified record is historical and cannot certify current health. An executed no-op runs read-only verification, and failure remains visible. JSONL audit records identify plans, edges and lifecycle results without storing router response bodies or credentials.

## Exact approval and daemon safeguards

Execution requires boolean `execute=True`, boolean `approved=True`, and the exact current plan ID. An explicit `approved_plan_id` can be supplied, or reconciliation can use the exact previously saved preview ID. A missing or materially changed plan is rejected. Plan IDs bind topology, demand, device, signaling expectations and rendered configuration. Input bytes are checked before and after parsing and again immediately before network access. The southbound boundary rechecks plan integrity and authorization immediately before writes. Optional approval expiration must be timezone-aware and unexpired, including executed read-only revalidation. The `NINJA_PCE_PREVIEW_ONLY` environment switch blocks all configuration paths.

Autonomous mode is disabled by default. Guarded-auto requires complete authoritative metadata and fresh per-edge provenance; a freshly touched file cannot refresh stale telemetry. Without adequate authority, a fresh file can produce a preview but cannot authorize configuration. Missing, malformed, future-dated and stale topology produce unhealthy cycles. Manual mode retains legacy file-based previews.

Daemon configuration rejects unknown keys, duplicate YAML/JSON keys, non-boolean booleans, non-finite numbers, negative timing/bandwidth limits, invalid ports and malformed lists. Command policy uses anchored vendor grammar in addition to configurable prefixes; widening a prefix cannot permit arbitrary command suffixes.

The defaults are:

```yaml
poll_interval: 5
retry_attempts: 3
retry_backoff_seconds: 5
retry_max_backoff_seconds: 60
debounce_seconds: 1
revalidation_interval_seconds: 30
```

Retries apply bounded exponential backoff to verification/recovery attempts, persist across restarts, and stop at the attempt limit until inputs change or an operator reviews and resets retry state. Uncertain configuration writes are not blindly retried. Debounce waits for stable material TE data; reordered snapshots and timestamp-only updates coalesce. Material TE changes reset the event revision. Verified no-op checks run at the revalidation interval. A search-budget exhaustion remains `indeterminate` / `SEARCH_LIMIT`, distinct from a proven no-path.

`/healthz` returns HTTP 503 before the first cycle and for stale, invalid, or degraded cycles. Metrics count operational verification failures separately, as well as revalidation, backoff and debounce. The endpoint reflects the last recorded cycle; the hosting process remains responsible for recording cycles and supervising its own liveness.

## Verification

All included tests use synthetic topology and operational evidence. Reproduce the reservation, restart, concurrency, approval, daemon and health regressions without router images:

```bash
.venv/bin/pytest -q tests/test_reconcile.py tests/test_daemon.py tests/test_reservations.py
.venv/bin/ruff check src/ninja_pce/reconcile.py src/ninja_pce/daemon.py src/ninja_pce/reservations.py src/ninja_pce/state.py tests/test_reconcile.py tests/test_daemon.py tests/test_reservations.py
.venv/bin/mypy src/ninja_pce/reconcile.py src/ninja_pce/daemon.py src/ninja_pce/reservations.py src/ninja_pce/state.py
```

Tests include two spawned processes competing for 100 Mbps, process restart with retained reservations, stale and contradictory observations, checksum corruption, partial-write recovery, zero-network preview/kill-switch behavior, stale approval, debounce, durable backoff and socket-free health responses. These are controller tests, not GNS3 integration or vendor qualification results.
