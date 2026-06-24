# ninja-pce design notes

## 1. Purpose

`ninja-pce` is a practical external CSPF helper and topology-driven configuration reconciler for RSVP-TE networks that do not have full native BGP-LS/PCEP/SR-TE controller capability everywhere.

The first useful release should help an operator answer:

> Given this topology, existing reservations, admin-groups, and service bandwidth demand, which explicit RSVP-TE path should be configured, which devices are affected, and how can the change be safely applied and verified?

## 2. Non-goals for early releases

The early tool is not:

- a full PCEP controller
- a stateful PCE
- an RSVP signaling engine
- a router head-end
- a QoS policer
- a system that performs unattended config pushes by default

## 3. Target architecture

```text
                   +----------------------+
                   |  FRR topology VM      |
                   |  OSPF/IS-IS listener  |
                   |  BGP-LS exporter      |
                   +----------+-----------+
                              |
                              | future normalized topology feed
                              v
+-------------+       +----------------+       +---------------------+
| YAML/JSON   | ----> | ninja-pce      | ----> | dry-run configs     |
| manual TED  |       | TED + CSPF     |       | plans/verifications |
+-------------+       | reconciler     |       +---------------------+
                      +---+--------+---+
                          ^        |
                          |        | approval policy
+----------------+        |        v
| text inventory | -------+  +----------------+
| hosts + OS     |           | Netmiko        |
+----------------+           | config push    |
                             +----------------+
```

## 4. Internal data model

The internal TED model is deliberately simple and adapter-friendly.

### Node

- `id`
- `loopback`
- `role`
- `area`
- `tags`

### Managed device

The operator must provide a text-based inventory that maps every configurable
topology node to its management endpoint and network OS.

- `name`
- `topology_node_id`
- `host`
- `port`
- `device_type` (a supported Netmiko platform)
- `username`
- `password_env` or another secret reference
- `tags`

Credentials should not be stored directly in the inventory.

### Link

- `id`
- `from`
- `to`
- `from_ip`
- `to_ip`
- `metric`
- `reservable_bw_mbps`
- `reserved_bw_mbps`
- `admin_groups`
- `srlg`
- `area`
- `protection`
- `bidirectional`

### Demand

- source PE
- destination PE
- bandwidth reservation
- setup/hold priority
- affinity constraints
- avoid/include constraints
- output template settings

## 5. CSPF behavior

The current CSPF algorithm is intentionally easy to explain in a 25-minute operator talk.

1. Build a directed graph from the topology.
2. Remove links with insufficient available bandwidth.
3. Remove links matching excluded admin-groups.
4. Keep only links satisfying include-any/include-all admin-group constraints when configured.
5. Remove avoided nodes, except source/destination.
6. Generate weighted shortest simple-path candidates.
7. Filter candidates by max hops, max metric, and required include-nodes.
8. Rank candidates by metric, then bottleneck bandwidth.
9. Render the selected path as an RSVP-TE explicit path.

This keeps the first version transparent enough for NOC/SRE/operator review.

## 6. Why candidate paths matter

Operators rarely want only one answer. During a maintenance or failure scenario, the tool should show alternatives:

- selected path
- second-best path
- lower metric but less headroom path
- higher metric but safer bandwidth path
- paths rejected because of constraints

The current release writes candidate path data into the CLI output and summary JSON.

## 7. Dry-run first

Every output should be reviewable before touching a router. The generated files are meant to support a change ticket:

- config snippet
- change plan
- pre-checks
- post-checks
- rollback reminder
- summary JSON

## 8. FRR integration direction

FRR should not be used as the RSVP-TE tunnel head-end in this project. Its role is topology visibility.

Possible Phase 2 path:

1. Run FRR on a Linux VM.
2. Join OSPF/IS-IS passively or as a non-transit listener where possible.
3. Collect link-state and TE attributes.
4. Export BGP-LS where possible.
5. Feed normalized topology JSON into `ninja-pce`.
6. Keep manual YAML input as a fallback.

## 9. Operator inventory

The inventory is mandatory for any workflow that can push configuration. YAML
is the initial format because it is human-readable, reviewable, and easy to
keep in version control.

Example:

```yaml
devices:
  - name: pe1
    topology_node_id: PE1
    host: 192.0.2.11
    port: 22
    device_type: cisco_ios
    username: automation
    password_env: NINJA_PCE_PE1_PASSWORD

  - name: pe2
    topology_node_id: PE2
    host: 192.0.2.12
    port: 22
    device_type: juniper_junos
    username: automation
    password_env: NINJA_PCE_PE2_PASSWORD
```

Before generating a push plan, `ninja-pce` must verify that:

1. Every affected topology node maps to exactly one managed device.
2. Every `device_type` has a supported renderer and Netmiko driver.
3. Required secret references are available.
4. The management endpoint is reachable during pre-checks.
5. The detected platform, when it can be checked, agrees with the inventory.

## 10. Southbound configuration

Netmiko is the first supported southbound mechanism. Other transports can be
added later, but the initial implementation should have one predictable push
path rather than several partially supported ones.

The minimum workflow is:

```text
compute path -> resolve inventory -> render config -> show diff
             -> run pre-checks -> apply approval policy
             -> push with Netmiko -> post-check -> record result
```

Dry-run remains available without device credentials. A real push requires a
complete inventory and explicit approval by default. Deployments may later
enable policy-based approval for narrowly defined changes.

## 11. Topology-triggered reconciliation

A meaningful topology change must trigger a reconciliation cycle:

1. Receive and normalize the new topology snapshot.
2. Compare it with the last accepted snapshot.
3. Ignore formatting, ordering, and other non-semantic changes.
4. Identify demands whose current or candidate paths are affected.
5. Re-run CSPF for those demands.
6. Render the desired configuration for each affected device.
7. Compare desired configuration with the last successfully applied state.
8. If there is a real config delta, run the southbound workflow.
9. Store the topology version, selected paths, config diff, approval, push
   result, and verification output.

A topology event triggers reconciliation, not necessarily an unattended
commit. The configured approval policy decides whether the generated update
waits for an operator or proceeds automatically.

The reconciler must debounce bursts of topology events, suppress no-op pushes,
serialize changes per device, and avoid reacting repeatedly to a flapping
topology. Failed pre-checks or incomplete inventory must stop the push without
changing the last known-good applied state.

## 12. Operator safety principles

1. Never hide pruning decisions.
2. Never perform unattended pushes in the default approval policy.
3. Always show verification commands.
4. Always show rollback reminders.
5. Keep RSVP-TE reservation separate from customer CIR policing.
6. Treat topology input freshness as a risk.
7. Prefer human-readable output over black-box automation.
8. Make topology-triggered pushes idempotent and auditable.
9. Refuse partial pushes when the affected device inventory is incomplete.

## 13. Production network operating model

If `ninja-pce` is placed on a real management network, it should run as a
closed-loop reconciler rather than as an interactive script.

```text
live topology feed     demand source        operator inventory
       |                    |                       |
       v                    v                       v
  normalize TED -----> validate intent -----> resolve devices
       |                    |                       |
       +--------------------+-----------------------+
                            |
                            v
                  compute desired state
                            |
                            v
                  policy + safety gates
                            |
           +----------------+----------------+
           |                                 |
           v                                 v
     preview/audit                  approved execution
                                             |
                                             v
                                  verify + record state
```

The production daemon should accept topology and demand changes continuously,
but it should only execute a device change after the approval policy and safety
gates agree that the change is allowed.

## 14. Autonomy levels

Autonomy must be explicit and visible in configuration.

### Level 0: offline planner

- Manual topology and demand files.
- No management network access required.
- Generates plans, configs, verification commands, and JSON summaries.

### Level 1: approved delivery

- Connects to routers only when `--execute --approve` or an equivalent API
  approval is present.
- Runs pre-checks, applies the rendered config, runs post-checks, and records
  the result.
- This is the default production-safe mode.

### Level 2: guarded autonomous reconciliation

- Runs as a daemon.
- Watches topology and demand sources.
- Automatically applies only low-risk changes that match a narrow policy.
- Sends every other change to preview/audit for operator approval.

### Level 3: service-intent controller

- Accepts service intent through an API.
- Owns demand lifecycle, selected paths, desired config, and applied state.
- May support richer transports such as NETCONF/gNMI/PCEP later.
- This remains a future direction, not the first network deployment target.

## 15. Guarded auto-approval policy

The default policy remains manual. An autonomous policy must be opt-in and
bounded. A change can be auto-applied only when all of these checks pass:

1. Inventory is complete for every affected topology node.
2. Credentials are present through secret references, never plaintext files.
3. The topology snapshot is fresh and has remained stable past debounce.
4. The affected demand has exactly one configurable head-end.
5. The candidate path is valid and explainable.
6. The diff touches only allowlisted RSVP-TE tunnel or explicit-path commands.
7. The diff size is below a configured command limit.
8. The new path does not violate max metric, max hop, affinity, SRLG, or
   protection requirements.
9. Residual bandwidth after reservation stays above a configured floor.
10. Device pre-checks pass.
11. The device is not locked by another pending or running change.
12. The number of autonomous changes per device and per hour is below a rate
    limit.
13. The change occurs inside an allowed maintenance or automation window when
    such a window is configured.

Any failed check downgrades the event to preview-only. The daemon must never
try to "partially" apply a change because only some checks passed.

## 16. Topology ingestion for real networks

Manual YAML remains the fallback source of truth, but a networked deployment
needs an adapter boundary:

```text
FRR / BGP-LS / parsed LSDB / device polling
                  |
                  v
        normalized topology snapshot
                  |
                  v
             semantic diff
                  |
                  v
              reconciliation
```

Adapters should produce the same normalized topology schema used by the manual
TED files. Raw FRR, BGP-LS, OSPF, IS-IS, NETCONF, or vendor CLI data should not
flow directly into CSPF. This keeps the CSPF engine deterministic and testable.

The first useful live adapter can be conservative:

1. Run FRR as a passive or non-transit listener where the lab allows it.
2. Export or parse LSDB and TE attributes.
3. Write normalized topology JSON atomically.
4. Let the existing reconcile loop consume that file.

Later adapters can read BGP-LS, NETCONF, gNMI, SNMP, or vendor CLI, but every
adapter should pass through the same normalizer and validator.

## 17. Autonomous reconciliation daemon

The daemon should manage many demands, not only one CLI invocation.

Responsibilities:

- watch one or more topology feeds
- watch a demand directory or service-intent API
- debounce bursts of topology events
- compute semantic fingerprints for topology, inventory, and demand state
- queue affected demands
- serialize per-device changes with locks
- suppress no-op config pushes
- apply the configured approval policy
- write audit records for previews, approvals, pushes, failures, and rollbacks
- expose health, status, and metrics

Minimum daemon inputs:

```yaml
topology_feed:
  type: file
  path: /var/lib/ninja-pce/topology/current.json
  max_age_seconds: 60

demands:
  type: directory
  path: /etc/ninja-pce/demands

inventory: /etc/ninja-pce/inventory.yml
state: /var/lib/ninja-pce/state.db
audit_log: /var/log/ninja-pce/audit.jsonl

approval_policy:
  mode: manual
```

The first implementation can keep JSON state files. A production daemon should
move to SQLite once it tracks many demands, pending approvals, retries,
rate-limits, and device locks.

## 18. Verification and rollback behavior

A networked system must verify that the router accepted the desired state and
that the RSVP-TE tunnel converged.

Pre-checks should confirm:

- management reachability
- platform identity
- route to the destination loopback
- current tunnel state
- current RSVP reservations when available
- no conflicting pending config session

Post-checks should confirm:

- the tunnel exists on the head-end
- the selected explicit path is installed or signaled as expected
- RSVP session state is up
- reservation bandwidth is visible
- the destination route or service binding remains reachable

Rollback should be generated before execution. Where the platform supports it,
the production workflow should prefer transactional or confirmed commits:

- Junos: commit check, commit, and rollback metadata
- IOS XR: commit replace or commit confirmed where appropriate
- IOS/IOS XE: archive or explicit removal commands when transactional commit is
  unavailable

If post-checks fail, the daemon should mark the demand as failed, alert, and
avoid repeated pushes until an operator clears the condition or a configured
rollback policy runs.

## 19. State, audit, and observability

Autonomy is only acceptable if the operator can reconstruct every decision.

Persistent state should store:

- topology version and fingerprint
- demand version and fingerprint
- inventory version and fingerprint
- selected path and candidate paths
- pruning decisions
- desired config hash and text
- last applied config hash and text
- approval decision and policy mode
- pre-check and post-check output
- push result or failure reason

Audit events should be append-only JSONL at minimum. Metrics should include:

- reconciliation cycles
- no-op suppressions
- previewed changes
- approved pushes
- autonomous pushes
- failed pre-checks
- failed post-checks
- no-path demands
- topology feed age
- queue depth
- per-device lock wait time

A small HTTP health endpoint can report daemon liveness, feed freshness, queue
depth, and the last reconciliation result.

## 20. Security model

Production deployment should assume the management network is sensitive.

Required controls:

1. Run with least-privilege router accounts.
2. Keep credentials in environment variables, a secret manager, or injected
   runtime files with restrictive permissions.
3. Never log passwords, private keys, or raw secret references beyond the
   reference name.
4. Keep generated config and audit logs permission-restricted.
5. Allowlist commands that the southbound layer may send.
6. Separate read-only discovery credentials from configuration credentials
   where the platform allows it.
7. Support a global kill switch that forces preview-only mode.
8. Make autonomous mode impossible to enable accidentally.

## 21. Failure handling

The daemon should treat uncertainty as a reason to stop at preview.

Examples:

- stale topology feed: do not push
- no path found: raise an actionable no-path event
- incomplete inventory: do not push
- missing secret: do not push
- failed pre-check: do not push
- failed config push: keep last applied state unchanged
- failed post-check: mark failed and alert
- flapping topology: debounce, then back off
- repeated failures for one demand: open a circuit breaker
- device lock timeout: retry later or preview only

The most important invariant is that failed or partial execution must not be
recorded as known-good applied state.

## 22. Implementation milestones for network autonomy

1. Keep the current offline planner and approved Netmiko delivery stable.
2. Add multi-demand reconciliation from a demand directory.
3. Add a daemon command with config file, health endpoint, and metrics.
4. Replace per-demand JSON state with SQLite-backed state and audit indexing.
5. Add explicit approval-policy configuration with manual and guarded-auto
   modes.
6. Add platform discovery and stricter pre-check validation.
7. Add rollback generation per renderer.
8. Add FRR/file-based normalized topology feed.
9. Add topology feed freshness checks and circuit breakers.
10. Add a read-only operator API for status, pending previews, and audit
    history.
11. Add an approval API only after authentication and authorization are clear.
12. Add richer topology adapters only after the normalized schema is stable.
