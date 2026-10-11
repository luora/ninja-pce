# Live PCEP failure-recovery qualification

On 2026-10-11, Ninja-PCE recovered PE1 Tunnel100 after a reversible P1–P2
core-link failure in the six-router Cisco IOS XE 16.9.7 GNS3 lab. Exactly one
controller-generated replacement PCUpd was followed by a matching ACTIVE report,
50 Mbps transit RSVP reservations and successful MPLS OAM. The interface was
restored, and the controller retained the replacement path.

This qualifies one withdrawal-driven recovery in this lab. It does not establish
multi-vendor support, general optimization or production autonomy. The
[machine-readable results](pcep_failure_recovery_validation_results.json)
contain the timeline, invariant checks and private artifact hashes.

## Deployed profile and baseline

The deployed source matched commit
`65e1ac6ad6174ac1652bfac07e251afc08bed3ee`; all 30 exported files were checked
against their SHA-256 manifest before deployment and again after qualification.
The isolated source export preserved the VM's existing Git checkout. Only the
optional [one-tunnel recovery profile](pcep_failure_recovery.md) was enabled.

The synchronized, fresh PE1 session reported D=1 for Tunnel100, PLSP-ID 524388,
with 50,000,000 bits/s requested bandwidth and setup/hold priorities 7/7. Its
healthy baseline was PE1 → P1 → P2 → PE3, with strict ERO
`10.0.12.2, 10.0.25.5, 10.0.56.6`. The validated TED had six nodes and 16
directed links. Recovery attempts and sent PCUpd counts were both zero.

IOS displayed D:0 in the CLI's LSP object while its current on-wire PCRpt
reported D=1; `show mpls traffic-eng tunnels tunnel 100` also reported Working
delegation yes. The controller used the synchronized wire report as its
authority. This CLI/wire discrepancy remains recorded in the private evidence.

## Failure and replacement

P1 GigabitEthernet2, the P1–P2 link, was shut administratively. This preserved
P1's controller-facing interface and the PCEP session. PE1 then reported a new
RSVP incarnation, LSP-ID 34, DOWN with an empty ERO and delegation retained.
The controller preserved the previously mapped healthy baseline.

| Observation | UTC time |
|---|---|
| Healthy baseline captured | 15:12:28.962 |
| New delegated DOWN/empty-ERO report | 15:12:31.037 |
| First retained graph with P1 → P2 withdrawn | 15:12:33.045 |
| Journal attempt timestamp | 15:12:39.026 |
| Replacement PCUpd sent, SRP-ID 1 | 15:12:39.075 |
| Correlated DOWN processing report | 15:12:39.115 |
| Correlated ACTIVE replacement report | 15:12:39.184 |
| Restored graph and retained path captured | 15:13:19.306 |

The failure graph contained 15 directed links: the specific baseline P1 → P2
edge had been withdrawn. After the configured five-second stable failure
interval, the controller sent exactly one `FAILURE_PATH` update. It selected
PE1 → P3 → P4 → PE3, with strict ERO
`10.0.13.3, 10.0.34.4, 10.0.46.6`, TE metric 30 and unchanged bandwidth and
priorities. The captured raw BGP-LS file's SHA-256 matched the update's exact
source digest:

```text
47b966bf1311a9c247973684a5d6ed9535dd0f04fe0e6bb264efe2cf125c1bed
```

The DOWN response did not complete recovery. The subsequent matching ACTIVE
report did, with the same PLSP, tunnel identity and SRP-ID. The journal attempt
count became one, pending state cleared, and the policy recorded `RECOVERED`.
The same PCEP run and session spanned baseline, failure and restoration.
The observed DOWN-to-PCUpd interval was 8.038 seconds, and PCUpd-to-ACTIVE was
0.109 seconds. These are one trial's control-plane measurements, not a
forwarding outage measurement or recovery-time guarantee.

## Independent verification and restoration

PE1 reported Tunnel100 UP, path valid and signaling connected, with dynamic
PCE option 1 as the basis for setup and SRP-ID 1 created via PCUpd. P3's
GigabitEthernet3 and P4's GigabitEthernet2 carried matching 50 Mbps RSVP
reservations for tunnel 100, LSP-ID 34.

`ping mpls traffic-eng tunnel 100 repeat 5 timeout 2` succeeded 5/5. MPLS trace
followed P3 `10.0.13.3`, P4 `10.0.34.4` and PE3 `10.0.46.6`, matching the
replacement ERO. The live dashboard API also displayed one acknowledged
`FAILURE_PATH` computation with that route, bandwidth and ACTIVE state.

The test's cleanup restored P1 GigabitEthernet2 without saving a temporary
shutdown. The interface reached UP/UP, its P2 OSPF neighbor returned to FULL,
and the TED returned to 16 directed links. After a 35-second restoration wait,
the replacement remained active, attempts stayed at one, and no second PCUpd
or controller switchback appeared. The retained capture spans 40.232 seconds
after the update; another MPLS echo passed 5/5 after restoration. The explicit
fallback remained configured and was not the basis for this recovery.

## Evidence and limits

Private console output, raw BGP-LS snapshots, checksummed PCEP captures and
journals remain under the Git-ignored
`build/live-lab-evidence/failover-2026-10-11/` directory. All retained state
checksums passed, and every retained raw graph reproduced its corresponding
derived topology. The published results retain artifact hashes while omitting
public addresses, console endpoints, MAC addresses, credentials and live
run/session UUIDs.

The journal records intent and attempts; it is not a reservation ledger. Its
attempt timestamp preceded the wire event, but this trial did not independently
sample the brief pending window. Deterministic socket tests verify durable
intent before wire delivery, progress-versus-terminal acknowledgment, delegation
revocation and kill-switch behavior at the writer lock.

The complete regression suite passed **719 tests**; Ruff and mypy passed for
all source and test files within their configured scopes. The suite's socket
tests required loopback network permission in the execution environment.

Concurrent failures, multiple controlled tunnels, pending-operation restart
recovery, other vendors, inter-area/inter-AS behavior and healthy-path
optimization remain outside this live qualification. Autoroute stayed disabled;
MPLS OAM verified the LSP, without qualifying customer service steering.
[Dashboard tunnel creation](dashboard_tunnel_creation.md) remains a proposed
feature requiring separate PCInitiate and RSVP-bandwidth qualification.
