# Repeatable six-router validation

**Device tests: NOT EXECUTED during remediation.** No licensed router images,
GNS3 server endpoint, or live telemetry were supplied. No router connection was
attempted. Offline computation and synthetic parser fixtures are separately
labeled; they cannot establish image support or operational interoperability.

## Physical and management plan

Use legally obtained IOS XRv/XRv9000 images whose forwarding/MPLS/RSVP features
are supported by the actual image. Initial CLI dialect target is IOS XR 7.9.x;
this is documented syntax, not a tested image qualification. Consult
[platform support](platform_support.md) for assumptions and primary sources.
Use an isolated GNS3 project; preserve the lab's previous router configuration.
FRR is an optional collector experiment, not an RSVP-TE replacement.

Connect all management interfaces to an isolated Ethernet switch. Management
subnet: `172.16.100.0/24`, Linux/Docker Ninja `172.16.100.10`, optional gateway
`.1`. Inventory currently maps the following routers; it is preserved unchanged.
Router interface names depend on appliance NIC order; record the actual mapping
before configuring. The table assigns deterministic logical port slots, not
invented appliance interface names.

| Router | Loopback /32 | Management | Core slots in ascending table order |
|---|---|---|---|
| PE1 | 10.255.0.1 | 172.16.100.101 | PE1-P1, PE1-P3, optional parallel |
| P1 | 10.255.0.2 | 172.16.100.102 | PE1-P1, P1-P2, P1-P3 |
| P3 | 10.255.0.3 | 172.16.100.103 | PE1-P3, P3-P4, P1-P3, optional parallel |
| P4 | 10.255.0.4 | 172.16.100.104 | P3-P4, P4-PE3, P2-P4 |
| P2 | 10.255.0.5 | 172.16.100.105 | P1-P2, P2-PE3, P2-P4 |
| PE3 | 10.255.0.6 | 172.16.100.106 | P2-PE3, P4-PE3 |

All core subnets use `/29`; do not copy `/30` onto subnets where endpoints .3,
.4 or .5 would be broadcast or belong to a different subnet.

| Resource | Local IP | Remote IP | TE metric | Reservable Mbit/s | Observed reserved Mbit/s |
|---|---|---|---:|---:|---:|
| PE1-P1 | 10.0.12.1 | 10.0.12.2 | 10 | 100 | 10 |
| P1-P2 | 10.0.25.2 | 10.0.25.5 | 10 | 100 | 80 |
| P2-PE3 | 10.0.56.5 | 10.0.56.6 | 10 | 100 | 10 |
| PE1-P3 | 10.0.13.1 | 10.0.13.3 | 25 | 200 | 20 |
| P3-P4 | 10.0.34.3 | 10.0.34.4 | 10 | 200 | 30 |
| P4-PE3 | 10.0.46.4 | 10.0.46.6 | 15 | 200 | 30 |
| P1-P3 | 10.0.23.2 | 10.0.23.3 | 25 | 100 | 10 |
| P2-P4 | 10.0.54.5 | 10.0.54.4 | 15 | 100 | 20 |
| PE1-P3-parallel (T04) | 10.0.113.1 | 10.0.113.3 | 20 | 200 | 20 |

The supplied TED reservations are simulation values, not router configuration
commands. Establish real background LSPs to produce those reservations, then
collect each direction's eight-priority unreserved vector. Alternatively use an
explicit administrative bandwidth restriction and update expected snapshots to
match it. Do not claim RSVP admission validation from a manually set reserved
counter in YAML. Preserve forward IDs; reverse resources use `:reverse`.

## Configuration sequence

1. Create the six router appliances and Linux/Docker management node, cable
   management and core links, record actual interface names, and save a GNS3
   project snapshot. Build Ninja with `bash gns3/scripts/build_ninja_pce_image.sh`.
2. Assign management/SSH access and loopbacks from the table. Supply secrets
   via the existing inventory's password environment references.
3. Initially use OSPF process100, area0 for all core interfaces and loopbacks,
   router-id from the loopback, point-to-point core network type, and TE router-id
   Loopback0. The generator normalizes all areas to0 for this qualification.
   The original multi-area sample remains available but inter-area TE requires
   separate platform qualification.
4. Enable MPLS-TE in the IGP and on every intended core link, RSVP on every core
   link, TE metrics and reservable bandwidth per direction from the table.
   Use `gns3/router-configs/ios-xr/` as starter references and the official release
   guide linked in platform support. Validate exact command availability on the
   legally supplied image before commit; the starter snippets are not golden
   complete router configurations.
5. Verify OSPF adjacency, reachability and TE advertisements before any LSP.
   Install controlled background reservations and collect a fresh authoritative
   TED. Only a collector with verified source ownership may label a snapshot
   `authoritative: true`, `complete: true`, with UTC `timestamp` and `source`.
6. Preview the case's computed directional IDs and rendered ERO, review the plan
   ID and pre-change configuration. For a new isolated LSP only, use explicit
   `--execute --approve --approved-plan-id ID`. Existing tunnel/path replacement
   is refused until ownership-aware recovery is qualified. Never bind customer
   traffic as part of these tests.
7. After each case collect evidence, restore the lab snapshot, and confirm
   baseline operational state before proceeding. Use distinct state roots only
   for distinct isolated networks; all demands within a network share one root.

## Prepare and run offline checks

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,southbound]'
.venv/bin/python gns3/validation/setup_lab.py --output build/gns3-validation
.venv/bin/python gns3/validation/run.py \
  --manifest build/gns3-validation/manifest.json --scope offline \
  --output build/gns3-offline-results.json
```

Setup writes isolated case inputs/manifest only; it never contacts GNS3 or
routers. Offline T10/T15 require operational evidence and are SKIP. The offline
runner checks case computations/accounting; full daemon churn, crash and device
behavior are covered separately by pytest mocks and later integration evidence.

## Integration test matrix

The generated manifest contains each condition, operator action, exact expected
directional link IDs and input files. T07 deliberately expects no feasible50M
path with P3 absent and the20M P1-P2 bottleneck.

| ID | Condition | Expected |
|---|---|---|
| T01 | 50M baseline | PE1-P3-P4-PE3 |
| T02 | 20M | PE1-P1-P2-PE3 when affinities permit |
| T03 | 50M bottleneck | P1-P2 pruned |
| T04 | Parallel PE1-P3 | PE1-P3-parallel selected and remote10.0.113.3 in ERO |
| T05 | Reverse PE3-P4 limited40M | Reverse50M takes PE3-P2-P4-P3-PE1 |
| T06 | PE1-P3 shut | PE1-P1-P3-P4-PE3 |
| T07 | P3 unavailable | Safe no-path for50M |
| T08 | Higher-priority preemption needed | Refuse automatic preemption |
| T09 | A80M + B80M / sole100M | B denied, aggregate never160M |
| T10 | LSP oper DOWN | Verification fails despite CLI acceptance |
| T11 | Unnumbered strict | Render/push refused |
| T12 | Stale collector | No autonomous configuration |
| T13 | Restart with pending A80M | Hold persists; no duplicate delivery; B denied |
| T14 | Repeat/flap | Debounce/backoff; no push storm |
| T15 | Router ERO differs | Verification fails |

## Collect and compare actual evidence

For supported path cases the collector is read-only and invokes only validated
show commands. Run explicitly against the isolated lab, after establishing the
manifest's condition. This command is not run during remediation:

```bash
.venv/bin/python gns3/validation/collect.py \
  --manifest build/gns3-validation/manifest.json --case T01 \
  --inventory gns3/lab-data/inventory/gns3_inventory.yml \
  --isolated-lab-id ninja-pce-six-router-isolated \
  --condition-evidence 'Baseline restored; OSPF/TE and reservation evidence saved in lab log' \
  --output-dir build/gns3-evidence
.venv/bin/python gns3/validation/run.py \
  --manifest build/gns3-validation/manifest.json --scope integration \
  --evidence-dir build/gns3-evidence --output build/gns3-integration-results.json
```

Evidence must include lab identity, timezone-aware capture time (at most300s
old), exact input SHA256 values, version output, raw command/output mapping,
and condition evidence. The runner compares golden links, computed links and
the detailed operational identity/destination/bandwidth/ERO. Missing evidence is
SKIP/NOT EXECUTED; unknown dialect, stale or ambiguous evidence is INCONCLUSIVE
or a verification FAIL. Neither a successful SSH connection nor config echo
proves an operational LSP. Fixture text under tests/fixtures is synthetic and
must not be supplied as integration evidence.

T03–T06 also require independent raw link, vector and failure-condition evidence;
matching the head-end LSP alone yields INCONCLUSIVE until condition adapters are
qualified. Operator condition text alone never establishes these scenarios.
Lifecycle cases require controller audit plus before/after router state, not
just one show response. The current evidence comparator marks T07–T09 and
T11–T14 INCONCLUSIVE when evidence is supplied; automated lifecycle evidence
qualification remains a next PR. They are still executable offline regressions
and have repeatable operator procedures. No unexecuted device case is PASS.

Command inventory: `show version`, OSPF neighbors/database, TE topology,
`show rsvp interface`, `show rsvp session`, per-tunnel detailed MPLS-TE output,
head-end explicit-path/interface running configuration, destination route,
and per-interface advertised priority vectors. Vendor commands selected by
`build_push_plan` are version scoped. Export each device's raw text and software
version; preserve confidential pre-change configuration in restricted storage.

Results JSON uses schema_version1, scope, lab_id, timestamp, and per-case id,
status (PASS/FAIL/SKIP/INCONCLUSIVE), reason, expected_links, computed summary
and evidence. The output's scope is mandatory when reporting results.
