# Bounded PCEP failure recovery

This optional lab profile watches one explicitly authorized delegated RSVP-TE
tunnel and supplies a replacement PCUpd after a validated link withdrawal.
It preserves the tunnel's endpoints, requested bandwidth, priorities and LSPA.
Healthy paths stay in place, including after a failed link returns. The default
listener and initial-path profile do not enable this behavior.

The [2026-10-11 live qualification](pcep_failure_recovery_validation.md) passed
one reversible core-link withdrawal on Cisco IOS XE 16.9.7, with correlated
ACTIVE reporting, independent RSVP/MPLS OAM and restoration without switchback.

## Execution boundary

Both peers must negotiate updates, and the PCC must finish synchronization and
currently delegate the tunnel. A fresh, healthy BGP-LS graph must first resolve
the reported UP/ACTIVE numbered strict IPv4 ERO into unique directional edges.
An initially unresolved or DOWN path cannot establish a healthy baseline.

A subsequent, newer validated graph must withdraw a recorded edge or explicitly
mark it down. Five seconds of stable failure evidence precede recomputation.
Metric changes, advertised free-bandwidth changes and collector timestamps alone
do not trigger an update. Replacement computation uses the full requested
bandwidth, without existing-reservation credit or preemption. This deliberately
under-admits where shared old/new edges cannot accommodate that conservative
amount. RSVP admission and signaling remain the PCC's responsibility.

The final boundary rechecks the live session, delegation, report fingerprint,
kill switch and exact topology digest inside the writer lock. It writes a
checksummed, atomically replaced journal intent before sending bytes. One update
may be pending; attempts are capped at three across restarts, with a 30-second
cooldown. A processing acknowledgment does not complete recovery. A correlated
response followed by a matching UP/ACTIVE intended route and bandwidth records
`RECOVERED`; this means control-plane convergence, not independent forwarding
verification. MPLS OAM remains a separate qualification observation.

No-path, stale/inconsistent sources and missing delegation send no replacement.
Unknown attributes, changed constraints, external path changes, errors, missing
terminal reports and uncertain writes stop automatic recovery. Pending state
survives restart and requires manual review; it is never blindly replayed.
An idle restart/reconnect requires a new synchronized healthy baseline. There
is no automatic rollback, deletion, resource release, PCInitiate or general
healthy-path optimization. This one-tunnel journal is not the shared reservation
ledger or distributed controller fencing.

Protocol behavior follows the narrowly used RSVP portions of
[RFC 8231](https://www.rfc-editor.org/rfc/rfc8231.html), its
[verified corrections](https://www.rfc-editor.org/errata/rfc8231),
[RFC 8786](https://www.rfc-editor.org/rfc/rfc8786.html) and
[RFC 9753](https://www.rfc-editor.org/rfc/rfc9753.html).
No broad protocol-conformance claim is made.

## Enable in the isolated lab

Install the reviewed code first. The additive example
[ninja-pcep-failover.conf](../gns3/controller/ubuntu-24.04/ninja-pcep-failover.conf)
extends the existing computation override for PE1 Tunnel100. The additional
options are:

```text
--failover-tunnel 10.255.0.1=100
--failover-state /home/ubuntu/ninja-pce/build/state/pcep-failure-recovery.json
```

Both are required. The tunnel must already be authorized by
`--compute-delegated-tunnel`; only one failure-recovery binding is accepted.
Journal, capture, node map and collector inputs must be distinct files.
Keep the listener on the lab NIC and preserve its explicit peer allowlist.
PCEP remains plain TCP; the public HTTPS dashboard does not protect PCEP.

After intentionally restarting the listener, wait for synchronization and check
fresh `D=1` reporting. An existing `D=0` tunnel does not become delegated merely
because this profile is configured. Any needed redelegation is a separately
observed PCC action. The retained Tunnel100 fallback is not evidence that a PCE
update succeeded.

The existing `NINJA_PCE_PREVIEW_ONLY=1` environment switch blocks both initial
delegated path writes and failure updates at their send boundaries. To disable
the profile, remove its optional override and restart the listener deliberately;
retain the journal and evidence for recovery review.

## Observe and qualify

The PCEP capture contains `failure_recovery` status, baseline, pending intent,
attempts and stop reason. Sent updates have status `FAILURE_PATH` with previous,
failed and replacement directional edge IDs and the source topology timestamp.
The read-only dashboard displays these updates in its computation history.

A complete live proof must retain dated evidence for:

1. Fresh synchronized delegation and a mapped healthy baseline; no healthy-path
   PCUpd or unexplained route change.
2. One reversible core-interface shutdown, followed by the exact BGP-LS edge
   withdrawal and a stable failure interval.
3. Exactly one controller-generated replacement PCUpd, its durable intent,
   processing response and matching replacement state.
4. Independent PE1 tunnel state, transit RSVP reservations and MPLS echo/trace
   matching that replacement. PCC fallback or local fast reroute alone cannot
   establish controller-driven recovery.
5. Restoration of the interface, OSPF/TE graph recovery and no automatic switch
   back to the healthy old route.

Restore the test interface even if qualification fails. Do not save a temporary
shutdown. On uncertainty, keep the journal, inspect fresh PCC/RSVP state, and
disable recovery while an operator resolves ownership and outstanding effects.
There is no automatic journal reset command.

## Regression checks

```bash
.venv/bin/pytest -q tests/test_pcep_failover.py tests/test_pcep_failover_server.py tests/test_pcep_compute_cli.py tests/test_pcep_delegation.py
.venv/bin/ruff check src tests
.venv/bin/mypy src/ninja_pce
```

These deterministic and loopback tests use synthetic failures and historical
wire fixtures. They do not substitute for the live proof above. Dashboard tunnel
creation is a separate [proposed feature](dashboard_tunnel_creation.md).
