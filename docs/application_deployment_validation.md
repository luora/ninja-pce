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

These are staging and preflight results. **The service cutover has not been
executed.** The old component processes remain the live deployment; the staged
application has not yet qualified their replacement.

The latest observed baseline contained six ACTIVE LSPs: a healthy legacy LSP
with delegation clear (`D=0`) and five with delegation set (`D=1`). The refined
baseline comparison preserves each existing delegation flag rather than
requiring every healthy LSP to be delegated. It grants no new authority and
does not force delegation of the legacy LSP. The final fresh baseline snapshot
is pending recovery of the authenticated controller console.

## Live migration status

**Pending.** No live result in this section is marked complete until observed
on the running controller. The existing domain-discovery lab evidence remains
the separate earlier qualification linked above.

Before migration, the Ubuntu console recovered from a reboot and all six
headend LSPs were UP. PE1 Tunnel10030 and PE4 Tunnel1003 each passed MPLS echo
5/5. These are baseline checks on the existing deployment, not results from
the unified application's activation or restart.

| Check | Status / required observation |
|---|---|
| Unified activation | Pending: one supervisor owns the collector, PCEP listener and dashboard; only their three replaced standalone owners stopped |
| Fresh topology | Pending: healthy exporter and consistent current raw/status/normalized inputs retain eight nodes and 22 directed links |
| PCC synchronization | Pending: all eight existing lab PCCs reconnect and synchronize |
| Existing LSPs | Pending: six previously active LSPs retain their current identities, EROs and delegation flags |
| Ownership preservation | Pending: original provisioning operation, reservation holds and managed recovery history unchanged |
| External services | Pending: GoBGP, qualified SSH provisioning, Nginx and authentication boundary retained |
| Dashboard | Pending: authenticated HTTPS view and read-only controller status agree with current observations |
| Restart behavior | Pending: healthy application restart preserves active paths without unnecessary PCUpd |
| Independent tunnel checks | Pending: router RSVP state and MPLS forwarding checked separately from PCEP reports |

Record actual counts, checked paths and any deviations after activation. Do not
infer forwarding from a healthy process, synchronized PCC or ACTIVE report.

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
