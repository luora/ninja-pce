# Live managed startup recovery — IOS XE 16.9.7

On 2026-10-14 UTC, the eight-router, two-area lab restored Tunnel1002 from PE1
to PE4 at 1 Mbps automatically after two controlled PE1 reloads using the final source. The saved tunnel
configuration remained in place. A fresh delegation triggered one PCEP PCUpd,
followed by a correlated ACTIVE report and successful MPLS echo, 5/5.

This extends the [explicit reinstatement trial](managed_tunnel_reinstatement_validation.md).
The [deployment guide](dashboard_provisioning_deployment.md#automatic-managed-startup-recovery)
documents the separate `--managed-startup-recovery` opt-in.

## Tunnel ownership and reboot failure

The dashboard originally created the router's `interface Tunnel1002` through
the IOS XE SSH configuration adapter. PE1 owns that configured tunnel and
signals its RSVP LSP; Ninja-PCE computes its path and delivers changes through
PCUpd. This is configured-tunnel delegation, not native PCInitiate creation.

Saving the configuration preserves its destination, bandwidth and dynamic PCE
path option. It does not preserve the controller-supplied runtime ERO. After
boot, PE1 reported an administratively enabled, delegated DOWN tunnel with an
empty ERO. Existing failure recovery required a previously adopted active path,
and the earlier explicit reinstatement request was already consumed. The
generic initial-delegation handler did not have authority to recreate this
verified managed operation. PE1's local area-0 TED also could not validate the
configured strict inter-area fallback's destination.

Startup recovery now uses the unchanged VERIFIED provisioning operation and
retained reservations to restore the exact configured profile. It waits for
live post-synchronization delegation and a fresh usable aggregated TED, then
debounces before sending through the existing guarded writer.

## Reconnect transition correction

The first live deployment exposed another transition: IOS XE can send a
post-synchronization DOWN report with delegation clear (`D=0`) before asserting
`D=1`. The receive path correctly invalidated live authority, but the periodic
watcher subsequently treated the retained D=0 report as a permanent managed
profile mismatch. This prevented the later valid delegation from recovering.

An exact undelegated or administratively disabled report now invalidates live
authority and waits. Sparse removal reports likewise invalidate the observed
path. Normalized availability flags are used only in a private copy to check
the reported profile; they never authorize computation or sending. Changed
constraints or immutable ownership still block. Withdrawal after an outstanding
intent retains uncertainty and cannot cause a replay.

The demonstrated zero-send false block was archived and reconciled only after
matching the captured D=0 profile, original operation, retained holds and zero
startup attempts. No attempt counter, creation authorization or reservation was
reset. This reconciliation is not an automatic bypass for existing blocked
journals.

A repeat trial also exposed a collector snapshot refresh between resource
preparation and durable intent. Refusing that stale path was correct, but
turning it into a permanent block was unnecessary: no intent, attempt or PCUpd
had occurred. The listener now discards that proposal, durably retains any
enlarged resource union and recomputes against a fresh TED. A persistence error
still stops recovery. The original no-intent block and unchanged startup history
were archived before the corrective deployment; no attempt was reset.

## Recorded result

| Check | Observation |
|---|---|
| Qualified router reloads with final controller source | Two |
| Requested / signalled bandwidth | 1,000,000 bit/s / RSVP 1000 kbps |
| Computed path / TE metric | PE1 → P3 → P4 → PE4 / 30 |
| Strict ERO | `10.0.13.3`, `10.0.34.4`, `10.0.48.8` |
| Delivery | One acknowledged `STARTUP_PATH` PCUpd per recovered session |
| Completion | Matching ACTIVE report; pending cleared; monitoring resumed |
| Forwarding | MPLS echo 5/5 after each final-source reboot; both traces matched the strict ERO |
| Startup history | Three durable attempts: one earlier variant and two final-source reboots; no resets |
| Existing failure counts | Managed tunnel two; legacy Tunnel100 three; unchanged |
| Original ownership and accounting | Provisioning journal and seven-edge held-resource ledger unchanged byte for byte |
| Independent RSVP checks | P3, P4 and PE4 each showed the same 1 Mbps reservation after the final reboot |
| Dashboard after recovery | Eight routers, 22 directed links, two active LSPs |

No manual path, tunnel bounce or new one-use reinstatement request was supplied
after either qualified reboot. The dynamic PCE option remained active. The source
bundle SHA-256 is
`ff5b6bf83e0e75234f154d215b005653d3a31fd48830add773e2eba50c2700cf`;
all 39 installed source files matched its manifest. Private captures, operation
and journal snapshots, wire evidence and console results remain outside Git.
Public addresses, credentials, MAC addresses and live operation/session IDs are
omitted here.

## Limits and regression checks

Startup delivery allows one send per PCEP session and at most three sends in a
rolling hour, separated by at least thirty seconds. The total is monotonic;
disabling the feature preserves its history. A pending write found on process
restart remains blocked without replay. Reaching the rate limit waits for the
window to expire; changed authority and uncertain writes require reconciliation.

The full suite passed **1,650 tests**; Ruff and mypy passed. Real socket tests
cover recurrent sessions, the five-second debounce, late TED convergence with
keepalives, retained D=0 reports, revocation behind the writer lock, pending
uncertainty, exact ownership and constraints, disabled startup, preview mode and
durable budgets. Additional socket cases refresh the TED after the ledger
write, verify durable resource retention before a successful retry, and verify
that a failed deferral-journal save stops retries without a send. Startup
recovery leaves lifetime failure counts intact.

This result covers this exact single managed RSVP-TE profile on IOS XE 16.9.7.
It does not establish recovery latency, throughput, customer traffic steering,
whole-controller VM reboot behavior, multiple concurrent managed tunnels,
native PCInitiate, NETCONF delivery, other vendors or inter-AS operation.
