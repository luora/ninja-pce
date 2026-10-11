# ninja-pce architecture

This view shows the current runtime shape of `ninja-pce`. Operator files or
live GoBGP snapshots supply the TE graph; the shared computation core selects
directional RSVP-TE paths. CLI delivery and bounded PCEP responses have separate
authorization boundaries. The dashboard observes files and cannot change routers.

```mermaid
flowchart LR
    subgraph client ["Users and automation"]
        operator["Operator"]
        gns3Lab["GNS3 Lab"]
    end
    subgraph gateway ["Command surface"]
        cli["CLI entry point"]
        dashboard["Read-only dashboard"]
    end
    subgraph service ["Application services"]
        core["Path computation core"]
        reconcile["Reconciliation engine"]
        daemon["Daemon and policy runner"]
        delivery["CLI delivery and verifier"]
        collector["Read-only BGP-LS collector"]
        pcep["PCEP observer and opt-in responders"]
    end
    subgraph datastore ["Files and state"]
        inputFiles["Topology, demand and inventory"]
        templates["Jinja Templates"]
        outputs["Configs, plans and summaries"]
        stateAudit["Local reservations, state and audit"]
        captures["Live snapshots and protocol captures"]
    end
    subgraph external ["Network and integrations"]
        routers["Managed routers and PCCs"]
        healthConsumers["Health/metrics consumers"]
        gobgp["GoBGP BGP-LS speaker"]
    end

    operator -->|"Runs Commands"| cli
    gns3Lab -->|"Demo Wrapper"| cli
    cli -->|"Compute Validate Push"| core
    cli -->|"Reconcile"| reconcile
    cli -->|"Daemon"| daemon
    core -->|"Reads Inputs"| inputFiles
    core -->|"Uses Templates"| templates
    core -->|"Writes Artifacts"| outputs
    reconcile -->|"Computes Desired State"| core
    reconcile -->|"Writes State"| stateAudit
    reconcile -->|"Plans Push"| delivery
    daemon -->|"Cycles Demands"| reconcile
    daemon -->|"Records Runs"| stateAudit
    delivery -->|"Reads Inventory"| inputFiles
    delivery -.->|"Netmiko Push Verify"| routers
    daemon -.->|"Serves Health Metrics"| healthConsumers
    routers -->|"BGP-LS"| gobgp
    gobgp -->|"Local read-only gRPC"| collector
    collector --> captures
    captures -->|"Fresh TED"| core
    routers -->|"PCRpt or PCReq"| pcep
    pcep -->|"Bounded computation"| core
    pcep -.->|"Opt-in PCRep, initial or failure PCUpd"| routers
    pcep --> captures
    operator --> dashboard
    captures --> dashboard
    stateAudit --> dashboard
```

## Runtime flow

```mermaid
flowchart LR
    topology["Topology TED"] --> loaders["loaders.py"]
    gobgp["GoBGP RIB"] --> collector["bgpls.py / live.py"]
    collector --> topology
    demand["Service Demand"] --> loaders
    inventory["Host OS Inventory"] --> loaders
    loaders --> models["models.py Dataclasses"]
    models --> cspf["cspf.py CSPF"]
    cspf --> render["render.py"]
    render --> artifacts["Config Plan Verification Summary"]
    models --> inventoryGuard["inventory.py"]
    inventoryGuard --> southbound["southbound.py"]
    render --> reconcile["reconcile.py"]
    reconcile --> southbound
    reconcile --> state["State JSON Audit JSONL"]
    southbound -.-> routers["Managed Routers"]
    reconcile --> daemon["daemon.py"]
    daemon --> policy["Guarded Auto Policy"]
    daemon --> health["Health and Metrics"]
    pcc["PCC reports and requests"] --> pcep["pcep.py"]
    pcep --> adapters["Bounded computation adapters"]
    adapters --> cspf
    pcep --> observations["Checksummed protocol captures"]
    topology --> dashboard["dashboard.py / web"]
    observations --> dashboard
    state --> dashboard
```

## Current boundaries

- Routers perform RSVP signaling and final admission. The PCEP listener observes
  by default; explicit bindings enable constrained PCReq/PCRep or initial PCUpd
  for delegated DOWN/empty-ERO LSPs. A separate opt-in profile replaces one
  authorized path after validated withdrawal of a previously mapped edge;
  initial-only configuration does not enable it. Unit/socket coverage and one
  successful IOS XE 16.9.7 lab withdrawal trial exist. That result does not
  qualify broader autonomous recovery. General active optimization,
  PCReq reoptimization and PCInitiate remain unsupported.
- CLI delivery is preview-first. A real push requires an exact reviewed plan ID
  with `--execute --approve --approved-plan-id`, or an explicit guarded-auto
  policy plus fresh, complete authoritative evidence and all delivery gates.
- The current live BGP-LS feed preserves directional TE attributes and withdrawals
  but cannot establish physical state or per-LSP ownership. It is non-authoritative
  for guarded automatic CLI writes. Static sample YAML is also non-authoritative.
- PCEP reports are not integrated with local reservation holds. Durable delegated
  ownership, authenticated PCEPS, distributed fencing and general recovery remain
  future work. The narrow withdrawal journal retains uncertain operations but is
  not the shared ledger or controller leadership. Filesystem locks coordinate one
  shared local state directory.
- Actual qualification covers a six-router CSR1000v IOS XE 16.9.7 lab. XR/Junos
  CLI dialects remain candidate adapters backed by documentation and mocks.
  A rendered configuration is not a platform or multi-vendor certification.
- The read-only dashboard distinguishes source freshness, reported state,
  acknowledgment and advertised RSVP reservations. It does not verify forwarding.
  The proposed headend/destination/bandwidth Create flow is not implemented.

See [implemented PCEP scope](pcep_computation.md),
[bounded failure recovery](pcep_failure_recovery.md),
[live withdrawal qualification](pcep_failure_recovery_validation.md),
[proposed dashboard creation](dashboard_tunnel_creation.md),
[live qualification](live_pcep_computation_validation.md),
[platform support](platform_support.md) and
[stateful ownership design](stateful_pce_architecture.md) for precise limits.
