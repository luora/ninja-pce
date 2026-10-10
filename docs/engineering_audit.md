# Engineering remediation audit

## Baseline and scope

Baseline commit: `bf0b004507d4ffd4f8de72edf63fdcb578f66bdb`.
Branch: `fix/te-cspf-reliability`. Inspection and baseline: 2026-10-09 (Africa/Nairobi).
Python 3.12.3, Git 2.43.0, NetworkX 3.7, PyYAML 6.0.3, Jinja2 3.1.6,
pytest 9.1.1, pytest-cov 7.1.0, Hypothesis 6.168.5, ruff 0.16.10, mypy 2.4.0.
An isolated `.venv` was created. Initial sandboxed installation failed DNS access;
the authorized installation succeeded with network access.
Optional delivery/lab dependencies were subsequently installed: Netmiko4.8.0,
ncclient0.7.1 and rich15.0.0. The installed XR/Junos commit and send_config_set
signatures were inspected without creating connections. `pip check` passes.

**Before code changes: `pytest -q`: 20 passed in 1.17s; zero failures/skips.**
All tracked package, tests, examples, tools, documentation and GNS3 assets were
inspected. There were no applicable AGENTS.md files. Existing uncommitted changes
in README, GNS3 documentation, entrypoint, inventory, and untracked architecture
and daemon assets were preserved; they are not remediation commits.

Execution: loaders → typed models → CSPF → render/templates → inventory-selected
push plan → Netmiko. Reconcile wraps semantic fingerprints, diffs and JSON state;
daemon discovers demands and invokes reconciliation under policy gates. Public
commands: compute, validate, inventory-validate, push, reconcile, topology-summary,
gns3-demo and daemon. Existing Mbps YAML and list/mapping inventory inputs are
compatibility surfaces. Unsafe legacy inputs must fail explicitly.

## Confirmed defects and risk

| Priority | Confirmed baseline defect | Consequence |
|---|---|---|
| P0 | DiGraph overwrites parallel resources | Selected node path can name the wrong physical link |
| P0 | Aggregate bandwidth ignores setup priority | Incorrect RSVP admission |
| P0 | Scalar metric minus bandwidth ranking | Large capacity defeats better metric |
| P0 | Fixed candidate enumeration cap | Feasible constrained paths reported impossible |
| P0 | Strict hops use router loopbacks | Exact adjacency and parallel member lost |
| P0 | bool/string/numeric coercion and missing validation | Unsafe or contradictory input accepted |
| P0 | Demands computed independently | Concurrent pending reservations overbook links |
| P0 | Accepted command execution treated as applied success | DOWN or ambiguous LSPs appear healthy |
| P1 | State saved after network change, without fsync/version integrity | Interrupted pushes lose pending ownership |
| P1 | No exact-plan authorization at push boundary | Stale approval and library-call bypass |
| P1 | Prefix allowlists and standalone legacy helper | Unsafe command execution bypasses controller policy |
| P1 | No unchanged-config operational revalidation | No-op suppression hides failed LSPs |

## Implemented corrections and design decisions

1. Directed edge adjacency preserves parallel resources, reverse overrides and
   selected link/IP identity in candidates, summaries and EROs. Immutable models
   retain metrics, max/reservable/reserved bandwidth, eight priorities, state,
   affinity/SRLG and provenance. Legacy bidirectional YAML expands deterministically.
2. Integer bit/s accounting rounds demand upward and capacity downward; conversion
   explicitly preserves Decimal precision. Cisco whole-kbps signaling is admitted
   and held at its actual rounded amount. Setup-priority observations remain
   distinct from conservative nonpreemptive admission and permission to preempt.
3. Lexicographic metric/headroom/hops/edge identity replaces scalar score. Iterative
   constrained DFS retains globally ranked top K; incomplete proof returns a
   distinct SEARCH_LIMIT, never a false NO_PATH or an unproven optimum.
4. Strict numbered IPv4 EROs use the chosen remote interface address. Unnumbered,
   IPv6 vendor EROs and ambiguous parallel addresses refuse rendering. Loose
   loopbacks preserve loose semantics and cannot confirm exact physical edges.
   XR and Junos hierarchy defects were corrected against primary documentation.
5. Strict loaders reject duplicate mapping keys/IDs, unknown fields, unsafe
   identifiers and scoped addresses, invalid metrics/priorities, malformed types,
   contradictory constraints/rates and quoted booleans. Compatible examples remain
   usable. See [schema and migration](input_schema.md).
6. A shared local resource lock covers computation, planning and delivery. The
   persistent ledger prevents independent demands from consuming the same free
   bandwidth. Owned authoritative observations can substitute already-accounted
   network debits; summed credits cannot exceed aggregate or priority7 debits.
   Old reroute resources and uncertain deliveries remain held.
7. Version-scoped operational adapters require identity, destination, positive
   admin/oper/signaling evidence, actual bandwidth and current route evidence.
   Duplicate/contradictory/unknown output fails. Commit output also needs positive
   acceptance; command transport is never operational proof.
8. Exact plan hashes bind TED, demand, inventory, command and signaling revisions.
   The actual execution boundary checks explicit approval, expiry, integrity,
   command grammar and kill switch. File revisions/freshness are checked again
   after prechecks immediately before writing. Preview makes zero network calls.
9. Schema2 state uses integrity checks, unique atomic temporary files and fsync.
   Pending state is durable before network contact. Recovery is read-only and
   never blindly repeats a possibly applied write. Unchanged executed plans are
   operationally revalidated. Bounded backoff/debounce and health track failures.
10. CLI pushes share the same ledger as reconciliation; the default resource root
    is `build/state` (`NINJA_PCE_STATE_DIR` or `--state-dir` may select a shared
    root). The legacy raw-file helper permits preview only. All same-network
    demands must share that root. See [reconciliation safety](reconciliation.md).
11. Deterministic CI runs pytest/coverage, ruff and mypy without router images.
    The GNS3 framework prepares T01–T15 snapshots and compares golden link IDs,
    computations and supplied operational evidence with explicit scope/status.
12. Structural adapter interfaces and [stateful PCE architecture](stateful_pce_architecture.md)
    prepare protocol ownership boundaries. Current RFC updates/errata were checked;
    no BGP-LS or PCEP network-control code was added.

## Compatibility and state migration

Existing source/destination, Mbps inputs, priorities, affinities and inventory
list/mapping forms remain where unambiguous. Original forward link IDs remain;
reverse IDs are `<id>:reverse`. Unsafe input coercion intentionally fails now.
The legacy score field remains display-only. The CLI adds shared-state and exact
approval options and exit3 for SEARCH_LIMIT. Initial execution requires a reviewed
preview ID; unchanged approved execution performs read-only checks.

Version1 state migrates for preview but cannot establish reservation ownership or
operational verification. Applied legacy state needs manual recovery before
execution. Checksums detect corruption, not malicious local edits. Correctly
checksummed invalid ledger values are also rejected. Documented automatic release
is intentionally absent until ownership/withdrawal evidence can prove it safe.

Baseline fake-output tests were updated where `ok`/`accepted` had previously
represented success. Synthetic detailed operational fixtures now distinguish
transport acceptance from VERIFICATION_FAILED and VERIFIED. This is an intentional
safety behavior change; no supported input example was removed.

## Validation results

The final machine-readable summary is [validation_results.json](validation_results.json).
Coverage XML and detailed GNS3 reports are generated under the local workspace.
Final run: **333 passed, zero failed/skipped, in19.96s; overall line coverage83%**
(2132/2576 statements). Ruff passes; mypy checks14 source files without issues.
Pinned local versions are in [validation_environment.txt](validation_environment.txt).
No production inventory credentials were modified and no routers were contacted.
GitHub Actions configuration was added; no remote CI execution is claimed.
The Python3.10 matrix is configured; local execution used Python3.12.3.

- Property tests compare 200 deterministic generated directed multigraphs with an
  independent exhaustive node-permutation/edge-product oracle. They check exact
  edge eligibility, directions, loop freedom, constraints, ranking and ordering
  invariance. 100 generated Decimal conversion cases check conservative rounding.
- Regressions include more than30 cheaper invalid waypoint alternatives,
  eight priorities, high-capacity metric ranking, competing80M/100M processes,
  partial-write/restart recovery, state corruption, stale telemetry, TOCTOU,
  unsupported versions, SSH loss, failed/unknown commits, DOWN/mismatched ERO,
  delayed establishment, no-network previews, backoff, debounce and health.
- The bundled50M baseline, avoid-link50M and120M demand commands ran successfully
  and retained their expected node paths; summaries now include exact edge IDs.
- GNS3 offline:13 PASS assertions,2 SKIP operational cases. GNS3 integration:
  **15 SKIP / NOT EXECUTED**, zero device PASS. No licensed images, GNS3 endpoint
  or live telemetry were supplied. Socket-free mocks cover the health handler.
- Existing shell scripts pass `bash -n`. Docker image construction and real
  RSVP/forwarding behavior were not executed or certified.

## Known limitations and acceptance boundaries

- Search is exponential in the general case, with a default100000 edge-expansion
  budget. SEARCH_LIMIT is explicit; large-network scalability remains future work.
- Preemption is deliberately disabled; a feasible path needing preemption remains
  unapplied. Eight priorities do not substitute for a live per-LSP inventory.
- Exact vendor EROs are numbered IPv4 only. IPv6, unnumbered links, inter-area TE
  signaling and release-specific formatting need separate qualification.
- Platform parsers and commits are documentation-checked and mock-tested, not
  certified against actual images. Current supported dialects are XR7.9.x,
  XE17.12.x and Junos23.4R. Unknown evidence fails closed.
- Existing TE object replacement and ownership-aware rollback/release are manual.
  Automatic merge/deletion is refused because stale trailing ERO hops or shared
  paths cannot safely be deleted without ownership/dependency proof. Confirmed
  commit uncertainty retains pending state, including positive-UP restart cases.
- Planned previews, failed changes and old reroute resources retain capacity.
  This can under-admit until an operator completes verified recovery; deleting
  the ledger is not a supported release procedure.
- Local filesystem locks coordinate cooperating processes sharing one directory.
  They are not distributed safety, and cannot coordinate manual router changes
  or another independent controller. A user-editable TED is not authenticated
  network telemetry. Final admission remains the router's responsibility.
- GNS3 lifecycle and raw failure-condition evidence adapters remain unqualified.
  T03–T06 cannot PASS from a matching head-end alone; lifecycle cases need reviewed
  audits and before/after state. Missing evidence stays SKIP; insufficient evidence
  stays INCONCLUSIVE. The runner's scope always distinguishes offline from device.

Acceptance criteria1–13 have local regression evidence within these explicit
boundaries; criterion14 has reproducible preparation/run instructions, while
actual licensed-image integration remains NOT EXECUTED. Criterion15 is this
limitations record. These statements do not certify autonomous production readiness.

## Reproduce

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,southbound,gns3]' -c docs/validation_environment.txt
.venv/bin/pytest -q --cov=ninja_pce --cov-report=term-missing --cov-report=xml
.venv/bin/ruff check src tests tools gns3/validation
.venv/bin/mypy
.venv/bin/ninja-pce compute --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml --output-dir build/acceptance/50m \
  --show-candidates --show-pruned
.venv/bin/python gns3/validation/setup_lab.py --output build/gns3-validation
.venv/bin/python gns3/validation/run.py --manifest build/gns3-validation/manifest.json \
  --scope offline --output build/gns3-offline-results.json
.venv/bin/python gns3/validation/run.py --manifest build/gns3-validation/manifest.json \
  --scope integration --output build/gns3-integration-results.json
```

## Recommended next pull requests

1. Licensed-image GNS3 qualification with redacted real operational/commit captures,
   interface inventory and per-scenario raw condition/lifecycle evidence adapters.
2. Ownership-aware transactional TE replacement, confirmed-commit crash recovery,
   rollback verification and explicit audited reservation release.
3. Read-only authoritative TED collection with complete per-LSP observations,
   withdrawals, provenance and freshness; compare it against device TED state.
4. Larger-network constrained-search improvements retaining proof/status semantics.
5. Independently scoped PCEP interoperability work after RFC update/errata review,
   passive state synchronization first, then negotiated delegated control.

## Remediation commits and changed files

- `b7a3160`: directed TE models, CSPF and strict validation.
- `84c6e0a`: RSVP renderers, exact delivery and operational evidence.
- `1e40db1`: shared reservations, durable reconciliation and daemon safeguards.
- `a1d4099`: pending confirmed-commit recovery stays unverified.
- Final validation/docs commit: CI, GNS3 runner, architecture and measured results.

Remediation changes only; original uncommitted user edits remain outside these commits:

```text
.github/workflows/ci.yml
README.md
docs/engineering_audit.md
docs/gns3_lab.md
docs/gns3_validation.md
docs/input_schema.md
docs/platform_support.md
docs/reconciliation.md
docs/stateful_pce_architecture.md
docs/validation_environment.txt
docs/validation_results.json
gns3/validation/collect.py
gns3/validation/run.py
gns3/validation/setup_lab.py
pyproject.toml
src/ninja_pce/cli.py
src/ninja_pce/cspf.py
src/ninja_pce/daemon.py
src/ninja_pce/interfaces.py
src/ninja_pce/inventory.py
src/ninja_pce/loaders.py
src/ninja_pce/models.py
src/ninja_pce/reconcile.py
src/ninja_pce/render.py
src/ninja_pce/reservations.py
src/ninja_pce/southbound.py
src/ninja_pce/state.py
src/ninja_pce/templates/change_plan.md.j2
src/ninja_pce/templates/generic_explicit_path.j2
src/ninja_pce/templates/iosxe_rsvp_te.j2
src/ninja_pce/templates/iosxr_rsvp_te.j2
src/ninja_pce/templates/junos_rsvp_te.j2
src/ninja_pce/templates/verification_commands.txt.j2
src/ninja_pce/verification.py
tests/fixtures/operational/iosxe_17.12_up.mock.txt
tests/fixtures/operational/iosxr_7.9_up.mock.txt
tests/fixtures/operational/junos_23.4_up.mock.txt
tests/fixtures/render/generic_50m.golden.txt
tests/fixtures/render/iosxe_50m.golden.txt
tests/fixtures/render/iosxr_50m.golden.txt
tests/fixtures/render/junos_50m.golden.txt
tests/test_cli_safety.py
tests/test_cross_safety.py
tests/test_daemon.py
tests/test_lab_validation.py
tests/test_loaders.py
tests/test_reconcile.py
tests/test_render.py
tests/test_reservations.py
tests/test_southbound.py
tests/test_te_core.py
tests/test_verification.py
tools/netmiko_dry_run.py
```
