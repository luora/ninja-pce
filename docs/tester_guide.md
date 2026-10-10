# Experimental lab tester guide

Colleagues can test computation, rendering, safety checks and historical
protocol replays without routers. This is an experimental lab preview;
production deployment and general platform certification are not claimed.
Some older README/status sections predate live BGP-LS/PCEP. Use this guide,
[input schema](input_schema.md) and [computation profiles](pcep_computation.md)
for the current testing boundary.

## Published data and history

The publication history replaces deployment Internet IPs with documentation-only
`192.0.2.0/24` examples, VM MACs with locally administered example addresses,
and controller session UUIDs in result summaries with stable pseudonyms.
Substitute your own Internet settings and actual NIC MACs before applying a
controller template. Public DNS service addresses are retained. Private lab
subnets, the generic six-router topology and protocol fixtures are retained to
make tests reproducible; they contain no Internet-facing console endpoints.

Unpublished commits were sanitized before publication, so their public commit
IDs differ from the original local qualification IDs mentioned in historical
results. Use `git rev-parse HEAD` when reporting tests of the published branch.
The original local raw evidence remains outside the published repository.

## Install and record your version

Clone the testing branch:

```bash
git clone --branch fix/te-cspf-reliability --single-branch https://github.com/luora/ninja-pce.git
cd ninja-pce
git rev-parse HEAD
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python --version
.venv/bin/python -m pip check
```

Python 3.10 and 3.12 are the CI targets; replace `python3.12` with `python3.10`
to test that version. Linux is the recommended starting environment. The
measured baseline was **583 passing tests, 85% coverage on Ubuntu/Python
3.12**; the final affected run passed 196 tests. Ruff and mypy passed for 19
source files. **Remote CI is pending**; local results do not establish it.
Dependency ranges are not a lockfile, so retain `pip freeze` with failures.

## Offline CLI checks

These commands use included example data and open no router connection:

```bash
.venv/bin/ninja-pce validate \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml \
  --inventory examples/inventory/network.yml
.venv/bin/ninja-pce compute \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml \
  --output-dir build/tester/computed --show-candidates --show-pruned
.venv/bin/ninja-pce push \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml \
  --inventory examples/inventory/network.yml \
  --output-dir build/tester/preview --state-dir build/tester/state
```

Expected 50 Mbps example path: **PE1 → P3 → P4 → PE3**, metric 50.
The P1–P2 bottleneck has only 20 Mbps. Inspect generated strict interface hops,
candidate/pruning details, configuration and plan ID. The `push` command above
is preview only; no credentials or Netmiko installation are needed. Do not add
execution flags to these tester examples.

Run the offline scenario matrix:

```bash
.venv/bin/python gns3/validation/setup_lab.py --output build/tester/matrix
.venv/bin/python gns3/validation/run.py \
  --manifest build/tester/matrix/manifest.json --scope offline \
  --output build/tester/offline-results.json
```

Expect 13 PASS and T10/T15 SKIP: those two require operational device evidence.
An offline PASS checks computation/accounting, not an installed router LSP.

## Regression and actual-capture replay

```bash
.venv/bin/pytest -q tests/test_bgpls_live_capture.py \
  tests/test_pcep_live_capture.py tests/test_pcep_live_computation.py
.venv/bin/pytest -q --cov=ninja_pce --cov-report=term-missing
.venv/bin/ruff check src tests tools gns3/validation
.venv/bin/mypy
```

The replay subset uses immutable bytes captured from the actual IOS XE/GoBGP
lab: topology/withdrawals, RSVP state reporting and initial PCRpt/PCUpd.
Replay freezes the topology clock explicitly; it proves neither current
freshness nor present-day forwarding. Operational `.mock.txt` files are
synthetic. Socket tests bind local loopback ports, not routers. Test counts
can change with the checked-out commit; report that commit with results.

## Pick a tester assignment

| Assignment | Useful checks and evidence |
|---|---|
| 1. Clean installation | Repeat on Python 3.10/3.12; run the commands above; report dependency, packaging or CLI problems. |
| 2. CSPF and inputs | Change a copied demand's bandwidth/affinities/waypoints; exercise parallel and asymmetric links; compare with `test_cspf.py`, `test_te_core.py`, `test_loaders.py`. |
| 3. Rendering | Review all three vendor goldens and bandwidth rounding; confirm missing numbered strict addresses are refused (`test_render.py`). |
| 4. Approval boundary | Run `test_cli_safety.py`, `test_southbound.py`, `test_verification.py`; probe stale approvals, kill switch, malformed output and secret redaction using mocks. |
| 5. Reconciliation/accounting | Run reservation, reconcile and daemon tests; investigate competing demands, no-op changes, pending holds and crash/restart recovery. |
| 6. Protocol robustness | Run BGP-LS/PCEP tests; investigate malformed/fragmented messages, session identity, stale sources, bounded work and old-LSP removal handling. |
| 7. Independent lab | Reproduce underlay, withdrawal/recovery and initial PCUpd with your own licensed images; retain raw dated router, RSVP and MPLS OAM evidence. |

For independent live work, start with [controller setup](initial_controller_setup.md),
[live protocol setup](live_protocol_setup.md), [PCEP computation](pcep_computation.md)
and [observed results](live_pcep_computation_validation.md). This is opt-in lab
work: GNS3, suitable host resources, reachable lab interfaces and **your own
licensed router images** are required. No router images are included. Service
files assume the documented Ubuntu user, paths and addresses; review them for
your environment. The CSR throughput limit means 50 Mbps RSVP reservation
is not a 50 Mbps throughput demonstration.

## Priority work remaining

- Complete independent live T02–T15 coverage in the
  [validation matrix](gns3_validation.md), including failure, asymmetry,
  bandwidth competition, stale source, restart, flapping and wrong ERO.
  Several lifecycle evidence comparators still return INCONCLUSIVE.
- Qualify active path replacement and PCReq reoptimization constraints
  (RRO, bandwidth type 2 and extensions), plus durable delegation recovery.
- Integrate observed PCEP/RSVP ownership with reservation accounting;
  BGP-LS remains non-authoritative for guarded automatic CLI delivery.
- Add authenticated transport/PCEPS, stronger peer authorization and broader
  vendor/release evidence. IOS XE 16.9.7 automated CLI writes remain blocked.
- Qualify inter-area/inter-AS paths and diversity/protection behavior.
  Customer QoS/CIR policy and measured throughput remain separate concerns.
- Choose a project license and pin tested dependency versions before broader
  distribution. The repository currently has no LICENSE or package license
  declaration; router-image licensing is separate.

## Bug report template

```text
Commit / branch:
OS / architecture / Python version:
Install command and relevant pip freeze:
Exact command and minimal input files:
Expected result:
Actual result / exit code:
Redacted stdout, stderr, traceback and generated summary:
Scope: offline mock / historical real capture / new isolated live lab
For live work: image/model/version, UTC time, condition, raw protocol/router evidence
```

Remove credentials, tokens, serial numbers and private environment details
before sharing. Include raw failure text as well as screenshots when possible.
