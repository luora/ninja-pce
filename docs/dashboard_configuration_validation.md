# Configuration provisioning qualification — IOS XE 16.9.7

On 2026-10-13 UTC, one private six-router GNS3 trial created **Tunnel1000 from
PE1 to PE3 at 1 Mbps** using the exact-plan configuration backend. IOS XE
configuration established the tunnel object and requested RSVP bandwidth; the
controller supplied its initial strict path through PCEP. The result passed
the headend RSVP checks, exact initial PCUpd completion correlation and separate
router reservation observations described below.

This qualifies one bounded lab profile. Native PCInitiate bandwidth creation
remains unqualified under the separate
[negative initiation results](dashboard_tunnel_creation.md#first-live-result--csr1000v-1697).
The shipped provisioning examples still default to disabled. Public deployment
requires its separate operator, HTTPS/Unix proxy and qualification gates.
The existing lab dashboard subsequently enabled this profile through its
authenticated HTTPS proxy. Anonymous dashboard, options and operation requests
returned the required Basic authentication challenge; the permissioned Unix
API served the qualified operator workflow. No account credential was exported.

## Executed profile

| Property | Recorded value |
|---|---|
| Platform | CSR1000v IOS XE 16.09.07 / IOS Software 16.9.7 |
| Headend / destination | PE1 `10.255.0.1` → PE3 `10.255.0.6` |
| New router object | Tunnel1000, independently allocated from managed IDs |
| Requested / signaled bandwidth | 1,000,000 bit/s; IOS XE configuration and RSVP display `1000 kbps` |
| RSVP priorities / affinity | 7/7; affinity `0x0`, mask `0x0` |
| Computed path | PE1 → P1 → P2 → PE3 |
| Strict remote-interface ERO | `10.0.12.2`, `10.0.25.5`, `10.0.56.6` |
| Primary / fallback | Dynamic PCE path-option 1 / generated explicit path-option 10 |
| Observed initial control | PCUpd SRP 1, corresponding current-session ACTIVE PCRpt |

The installed policy permits only PE1 → PE3, caps signaled bandwidth at 1 Mbps
and admits at most one held managed tunnel. The live trial exercised exactly
1 Mbps; smaller requests have offline admission/unit-conversion coverage rather
than additional live qualification. General headend/destination selection is
subject to the configured allowlists.

The reviewed SHA256 plan binds the generated configuration, headend inventory,
reservation amount, strict fallback and verification expectations. Current
topology, capacity, policy, plan expiry and scoped resource absence are checked
before the single configuration write. The PCEP writer obtains authority for
that exact new tunnel only after those prechecks. Its initial update must retain
the approved route, identity and constraints.

## Independent observations

The detailed PE1 response established all five required headend checks:

| Check | Observed evidence |
|---|---|
| Identity | Exactly Tunnel1000 |
| Destination | `10.255.0.6` |
| Bandwidth | Configured `1000 kbps`, Path Tspec and Resv Fspec each `1000 kbits` |
| Ordered route | Current RSVP Path ERO follows P1/P2, with the IOS XE destination-loopback display suffix |
| Signaling | Admin/Oper up, Path valid, Signalling connected |

PE1 also showed dynamic path-option 1 as the setup basis, working delegation
to `172.16.100.10`, and **Created via PCUpd message** from that controller.
Its PCC display reported requested and used bandwidth of 125,000 bytes/s,
which equals 1 Mbps. Controller evidence separately correlated the approved
initial-path update and ACTIVE report for the same plan, session and LSP identity.
The backend reported `VERIFIED` with scope `headend-rsvp`.

Read-only sender/reservation captures from all six routers independently bound
the new reservation to the source, destination, tunnel ID 1000 and RSVP LSP ID 1:

| Router | Tunnel1000 evidence |
|---|---|
| PE1 | 1 Mbps sender and reservation; next hop `10.0.12.2` |
| P1 | 1 Mbps sender from `10.0.12.1`; reservation toward `10.0.25.5` |
| P2 | 1 Mbps sender from `10.0.25.2`; reservation toward `10.0.56.6` |
| PE3 | 1 Mbps sender from `10.0.56.5`; terminating reservation |
| P3 / P4 | No Tunnel1000 reservation; existing Tunnel100 remained at 50 Mbps |

The existing Tunnel100 configuration and explicit fallback were preserved.
It remained operational at 50 Mbps on PE1 → P3 → P4 → PE3 during this trial.
The new operation did not own or replace that resource.

The sanitized
[positive operational fixture](../tests/fixtures/operational/iosxe_16.9.7_configured_tunnel1000_up.live.txt)
preserves the observed Tunnel1000 attributes and PCE-controlled RSVP route.
Its real managed names and time fields were redacted; private lab addresses
remain so the path can be tested. Regression tests require the current upper
route rather than the separately displayed unconstrained lower route, matching
Tspec/Fspec, and the observed PCE selection/delegation/update indicators.
Private raw console captures and controller evidence remain outside Git.

## Limits and cleanup

This trial establishes object creation, a 1 Mbps RSVP reservation and initial
PCEP path control on this exact image and bounded profile. It does not establish
payload forwarding, end-to-end OAM, throughput, customer traffic steering,
multi-vendor behavior or general failure recovery/reoptimization of newly
managed tunnels. Tunnel100's previously qualified withdrawal-recovery profile
remains a separate workflow. The public UI's headend verification label does
not imply that every future operation receives independent transit inspection.

**Qualification cleanup passed.** Exact owned configuration checks preceded
removal of Tunnel1000 and its generated explicit path. Fresh sender/reservation
tables from all six routers, absent headend configuration and the fresh
synchronized PCC database independently confirmed removal. Tunnel100's
configuration matched its pre-test baseline, and PE1's approved SSH setup was
saved after cleanup. The operation was durably recorded as `ROLLED_BACK` before
only its matching controller reservation was released; its verified test history
was retained. Managed IDs are not reused.

After release, all six controller services were active and the operator API
accepted a read-only 1 Mbps preview for Tunnel1001 over PE1 → P1 → P2 → PE3.
A mutation request without the required session/origin/CSRF credentials was
rejected with HTTP 403. This final readiness check sent no router configuration.

A restarted controller, removal PCRpt or missing GUI line alone does not release
a hold. There is no public deletion endpoint or automatic teardown.
