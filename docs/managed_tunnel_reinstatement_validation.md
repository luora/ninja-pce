# Live explicit managed-tunnel reinstatement — IOS XE 16.9.7

On 2026-10-14 UTC, the eight-router, two-area lab reinstated the previously
verified Tunnel1002 from PE1 to PE4 at 1 Mbps through one PCEP PCUpd. PE1
reported `Admin: up`, `Oper: up`, dynamic PCE option 1 active and RSVP signalling
connected. MPLS echo passed 5/5; MPLS trace followed PE1 → P3 → P4 → PE4.

This extends the [inter-area provisioning trial](multi_area_tunnel_validation.md).
The [deployment guide](dashboard_provisioning_deployment.md#explicit-reinstatement-after-the-active-path-is-lost)
describes the local review and authorization commands.

## Cause and correction

The tunnel was administratively enabled but operationally DOWN, delegated with
an empty ERO. Its recovery baseline was absent. Existing failure recovery could
not bootstrap that managed tunnel, and a later router bandwidth change to
1 kbps differed from the immutable 1 Mbps provisioning profile.

The lab restored the original `1000 kbps` configuration and deployed an explicit
one-use reinstatement request. Review binds the VERIFIED operation, retained
holds, recovery journal revision, current session/report, existing attempt count
and computed strict path. General initial-delegation authority remains scoped
separately; the original creation authorization is not renewed or replayed.

A live finding exposed a serialization bug: fingerprints used `repr()` for
nested report dictionaries. A sorted JSON capture changed those fingerprints
despite describing the same wire report. Canonical JSON digests now preserve
the fingerprint across that boundary while still detecting changed constraints
and ordered wire values. A real socket regression exercises the capture round
trip before authorizing reinstatement.

## Recorded result

| Check | Observation |
|---|---|
| Requested / signalled bandwidth | 1,000,000 bit/s / RSVP 1000 kbps |
| Computed path / metric | PE1 → P3 → P4 → PE4 / 30 |
| Strict ERO | `10.0.13.3`, `10.0.34.4`, `10.0.48.8` |
| Delivery | One acknowledged `REINSTATEMENT_PATH` PCUpd; PE1 confirmed PCUpd creation |
| Completion | Correlated ACTIVE report; pending intent cleared; monitoring resumed |
| Recovery budget | Existing count advanced from one to two of three permitted sends |
| Ownership and holds | Original provisioning journal and seven-edge held-resource ledger unchanged byte for byte |
| Independent RSVP checks | P3, P4 and PE4 showed the same 1 Mbps reservation, terminating at PE4 |
| Forwarding | MPLS echo 5/5; trace matched all three strict ERO hops |
| Other tunnel | Tunnel100 remained up; its configuration and separate attempt count were preserved |

The restored Tunnel1002 profile already matched startup configuration. Deployment
restarted only the PCEP listener; dashboard, provisioning, collectors and Nginx
processes stayed running. Private request, capture and recovery evidence remain
on the controller outside Git. The final deployed source bundle SHA-256 is
`f178298810cc2e3485aff4f4b044077a92ec24cbf03e9556d7278db278186d61`.

The full suite passed 1,580 tests; Ruff and mypy passed. This result qualifies
explicit reinstatement of this exact single-tunnel IOS XE profile. It does not
qualify unattended recovery after a router reboot, general reconciliation of
uncertain writes, repeated failures, throughput or other vendors.

The later [managed startup recovery trial](managed_startup_recovery_validation.md)
separately qualifies automatic recovery of the saved profile after a router reload.
