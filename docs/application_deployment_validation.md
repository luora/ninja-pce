# Generic application packaging and deployment validation

This record covers Ninja-PCE 0.5.0 packaging and the transition from separately
started collector, PCEP and dashboard processes to the unified controller
application. It was prepared on 2026-10-15 (Africa/Nairobi).

The application assembles the existing protocol engine; it does not expand
PCEP authority, enable SSH provisioning by default or qualify another router
platform. See the [deployment guide](deployment.md),
[user guide](user_guide.md) and
[topology-domain validation](topology_domain_validation.md).

## Source checkpoints

| Checkpoint | Change |
|---|---|
| `ab5e0a4` | Configurable controller application, version 0.5.0 package, deployment tools and guides |
| `3886b08` | Build provenance captured before scratch artifacts, with source changes still detected |
| `b1e29a1` | CI selects and preflights a working native headless Chrome executable |

These checkpoints are on `feat/generic-controller-application`. Distribution
manifests record the full source revision, source cleanliness, Python/platform,
dependency-download choice and artifact hashes. A wheel or clean build record
does not establish live router behavior.

## Completed local checks

| Check | Result | Scope |
|---|---|---|
| Latest complete regression suite | 1,876 passed in 199.66 seconds; 88% coverage | Application integration and existing protocol/computation/provisioning behavior after the browser harness fix |
| Focused deployment/provenance checks after the final provenance change | 33 passed | Release assembly, installer and source provenance behavior |
| Browser harness selection | 7 passed | Explicit native Chrome selection and startup preflight; existing UI assertions retained |
| Ruff | Passed | Static checks for the reviewed implementation and tests |
| mypy | Passed | Type checks for the application source |
| Offline wheel smoke | Passed | Built application wheel exercised with local runtime dependencies |
| Final release integrity | Passed | Built from pushed `3886b08`; manifest records `source_dirty=false`, checksums and wheel/source bytes match |
| Fresh offline installation | Passed | Isolated wheel installation passed version, configuration validation, command planning and status checks |
| Empty-input behavior | Passed | Missing live sources produce degraded status with exit code 1; static validation creates no live, state or runtime directories |
| Guide configuration example | Passed | Embedded controller/domain JSON validated with the real parser; all three component commands produced |
| Documentation checks | Passed | New guide links, fenced blocks, CLI syntax and sensitive-lab pattern checks |

The complete suite includes the final browser harness correction. The earlier
focused provenance checks are a separate run, not another complete-suite result.

Five browser tests previously timed out during browser startup in remote CI.
The correction selects an explicit native Chrome executable and preflights its
headless startup before running the browser tests. All five existing UI test
assertions remain unchanged; the correction repairs the execution harness.

[Remote CI run 38717359767](https://github.com/luora/ninja-pce/actions/runs/38717359767)
passed its packaging, Python 3.10 and Python 3.12 jobs. The release wheel was
built from `3886b08`; `b1e29a1` changes the CI/browser test harness only.

The release provides the wheel, source archive, reviewable installer,
configuration/domain/speaker examples, constrained runtime dependency bundle,
guides, artifact checksums and distribution manifest. The GoBGP speaker remains
an external dependency. The packaged read-only protobuf schema removes the
collector's dependency on a source-checkout schema path.

The installer defaults to rendering four review files. Rendering performs
deployment-shape, path and artifact checks; it is not the complete controller
validator. Explicit installation resolves runtime dependencies offline and
validates the supplied controller/domain documents in a temporary environment
before persistent installation. It never starts/enables the service, opens a
firewall, configures routers or overwrites an existing release.

## Live host staging

Staging on the existing Ubuntu controller completed these checks:

| Check | Result / scope |
|---|---|
| Runtime bundle integrity | Passed: the transferred release bundle was verified before installation |
| Offline versioned installation | Passed: version 0.5.0 installed into a new virtual environment using local wheels |
| Component command comparison | Passed: effective old/new collector, PCEP and dashboard commands were compared before cutover |
| Managed startup policy | Passed: the existing managed startup binding/digest passed preflight with the staged application |
| Durable ownership preflight | Passed: existing journal and reservation-hold hashes matched their retained baseline |

These staging and preflight checks preceded the completed activation recorded
below. They are separate from protocol, restart and forwarding observations.

The latest observed baseline contained six ACTIVE LSPs: a healthy legacy LSP
with delegation clear (`D=0`) and five with delegation set (`D=1`). The refined
baseline comparison preserves each existing delegation flag rather than
requiring every healthy LSP to be delegated. It grants no new authority and
does not force delegation of the legacy LSP. A fresh baseline was captured
before activation for the comparisons below.

## Live migration status

**Activated.** Ninja-PCE 0.5.0 from source `3886b08` now runs under the enabled
`ninja-controller` service. A healthy service restart and HTTPS authentication
boundary checks passed. Post-migration PE1 forwarding checks passed; the reverse
PE4 check is pending console access. The existing domain-discovery lab evidence
remains the separate earlier qualification linked above.

Before migration, the Ubuntu console recovered from a reboot and all six
headend LSPs were UP. PE1 Tunnel10030 and PE4 Tunnel1003 each passed MPLS echo
5/5. These are baseline checks on the existing deployment, not results from
the unified application's activation or restart.

| Check | Status / required observation |
|---|---|
| Unified activation | Passed: one supervisor owns the collector, PCEP listener and dashboard; only their three replaced standalone owners stopped; new unit enabled |
| Fresh topology | Passed: healthy current input set contains eight nodes and 22 directed links |
| PCC synchronization | Passed: all eight PCCs are fresh and synchronized |
| Existing LSPs | Passed: six ACTIVE LSPs; all routes and delegation flags match the fresh baseline; no PCUpd sent |
| Ownership preservation | Passed: provisioning journal and reservation-hold hashes unchanged; managed authority, startup/failure attempts and history unchanged |
| External services | Passed: unrelated external service process IDs unchanged |
| Controller status | Passed: application observes eight PCCs, six ACTIVE LSPs and the original single reservation |
| HTTPS boundary | Passed: unauthenticated HTTPS `HEAD /healthz` returned HTTP 401 with `WWW-Authenticate`; no authenticated session tested |
| Restart behavior | Passed: healthy application service restart retained eight fresh synchronized PCCs, six ACTIVE LSPs, eight nodes, 22 directed links and one reservation |
| Restart route/authority preservation | Passed: all six routes and delegation flags identical; no updates or archived updates; journal/hold/managed history and unrelated service process IDs unchanged |
| Boot enablement | Passed: `ninja-controller` enabled; no cold VM boot qualification claimed |
| Managed PE1 tunnel | Passed: Tunnel1002 Admin/Oper UP, RSVP signalling connected and MPLS echo 5/5 |
| Generic PE1 tunnel | Passed: Tunnel10030 MPLS echo 5/5 after migration/restart |
| Reverse PE4 tunnel | Pending: post-migration forwarding check awaits its current console endpoint |

The retained baseline managed history contained two failure attempts and nine
startup attempts, including operator reboots before migration. Activation and
the service restart did not reset or advance that history. Healthy legacy `D=0`
and the other five `D=1` reports retained their existing authority boundaries.

These checks qualify the application service transition and restart, not a cold
controller VM reboot. The HTTPS observation establishes the anonymous challenge
only; no password was read and no authenticated browser session was exercised.
Forwarding results come from the named MPLS echo checks, separately from PCEP
reports. The reverse PE4 result remains pending.

## Scope

This release consolidates configuration, supervision, status and deployment.
Generic domain policy continues to authorize bounded initial paths for eligible
router-owned tunnels and passive adoption of healthy LSPs. Exact managed
failure/startup recovery retains its separate operation, journals, held resources
and attempt limits. The packaging does not turn that profile into recovery for
every discovered tunnel.

Native bandwidth-aware RSVP PCInitiate, general active reoptimization,
multi-vendor certification, production transport authentication and clean-host
production deployment remain outside this record. Live migration of an existing
qualified lab is distinct from validating every step on a new Ubuntu host.

Live addresses, credentials, operation UUIDs, session identifiers and private
evidence paths are omitted.
