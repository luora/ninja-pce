# Delegation through topology-domain discovery

Configure the managed topology domain once with `--compute-domain-policy PATH`.
Ninja-PCE then discovers eligible PCCs from its current validated BGP-LS TED.
New routers and tunnel IDs do not require controller allowlist entries,
headend mappings or listener restarts. Each router still needs its own routed
PCC configuration and a supported tunnel configuration.

The policy grants bounded initial-path authority for router-owned tunnels.
The PCE sends PCUpd; the router performs RSVP admission and signalling. This
mode does not create interfaces, use PCInitiate or register tunnels as
dashboard-owned provisioning operations.

The [lab validation record](topology_domain_validation.md) separates completed
live checks from pending qualification work.

## Configure the domain once

The [example policy](../examples/pcep/topology-domain-policy.json) uses
invented private addresses and an example private AS. Replace its values with
the domain identities and address ranges advertised by the deployment's
trusted BGP-LS exporters:

```json
{
  "version": 1,
  "domain": {
    "protocol_id": 3,
    "instance_id": 0,
    "autonomous_system": 65000,
    "bgpls_identifier": 0
  },
  "pcc_prefixes": ["10.200.0.0/16"],
  "destination_prefixes": ["10.200.0.0/16"],
  "max_bandwidth_bps": 100000000,
  "max_live_lsps": 32
}
```

| Field | Meaning |
|---|---|
| `version` | Policy schema version; currently `1` |
| `domain.protocol_id` | Exact BGP-LS IGP protocol identity; `3` denotes OSPFv2 |
| `domain.instance_id` | Exact BGP-LS NLRI instance identifier |
| `domain.autonomous_system` | Exact AS node descriptor |
| `domain.bgpls_identifier` | Exact BGP-LS identifier node descriptor |
| `pcc_prefixes` | Approved IPv4 PCC source and TE router-ID ranges |
| `destination_prefixes` | Approved IPv4 tunnel destination ranges |
| `max_bandwidth_bps` | Positive integer bandwidth cap per LSP, in bits/s |
| `max_live_lsps` | Bounded concurrent LSP admission count per PCC |

The four domain identity fields are separate from the OSPF area. Nodes in
multiple areas can belong to the same approved domain. Existing explicit ABR
identity projection remains necessary where duplicate area advertisements
must be combined into one physical router; domain discovery does not infer
ABR equivalence from matching addresses alone.

Add this option to the existing listener's reviewed computation configuration:

```text
--compute-domain-policy /etc/ninja-pce/topology-domain-policy.json
```

Keep the raw BGP-LS, collector status, normalized topology and trusted exporter
inputs. Keep any existing exact recovery/provisioning options and their
journals. Per-router `--allow-peer` and `--compute-headend` entries remain
available for those legacy profiles; they are not required to register each
new domain-discovered PCC. A node-name map supplies optional readable labels
for newly discovered routers rather than granting them authority.

Use strict CIDRs that name network addresses, such as `10.200.0.0/16`.
The policy is loaded at listener startup; changing its scope requires a
restart. Newly advertised eligible routers within unchanged scope do not.
Each scope permits 1–64 prefixes, the bandwidth cap is at most `10^12` bits/s,
and the per-PCC LSP cap is 1–256. Unknown or missing fields reject startup.

## Configure every router once

1. Establish OSPF, MPLS-TE and RSVP underlay reachability. The source,
   destination and complete directed TE path must appear in the healthy
   BGP-LS feed under the approved domain.
2. Configure a global stateful PCC peer. Its TCP source must equal that
   router's advertised IPv4 TE router ID/loopback for this discovery profile.
   A tunnel's `dynamic pce address` option alone does not establish a PCC
   session.
3. Provide routed reachability to the controller through the lab control
   network. Apply transport access controls to the approved PCC ranges.
4. Confirm the PCEP session establishes and completes synchronization.
   Configure as many supported tunnels as the policy permits; no controller
   edit is needed for each new interface number.

The IOS XE 16.9.7 router profile uses this global fragment; replace the
placeholders with routed addresses for that router:

```text
mpls traffic-eng pcc peer <PCE_ADDRESS> source <TE_ROUTER_ID> delegate-only
mpls traffic-eng pcc report-all
```

Each router-owned tunnel uses the usual configuration:

```text
interface Tunnel<TUNNEL_ID>
 description <SERVICE_DESCRIPTION>
 ip unnumbered <TE_LOOPBACK_INTERFACE>
 tunnel mode mpls traffic-eng
 tunnel destination <DESTINATION_TE_ROUTER_ID>
 tunnel mpls traffic-eng priority 7 7
 tunnel mpls traffic-eng bandwidth <POSITIVE_KBPS>
 tunnel mpls traffic-eng affinity 0x0 mask 0x0
 tunnel mpls traffic-eng path-option 1 dynamic pce address <PCE_ADDRESS>
```

The tunnel's IPv4 sender must match the PCC source. Both endpoints must
identify unique current nodes in the approved domain and satisfy their
respective address scopes. A router acting only as transit or destination
does not automatically establish a PCC session; it needs the global setup
when it begins originating tunnels.

## Authority and runtime behavior

Discovery uses validated current binary BGP-LS identities, not display names,
archived dashboard nodes or the presence of an address in a CIDR alone.
Unrelated AS, IGP-instance, protocol and BGP-LS identifier advertisements do
not grant authority. A stale or incomplete TED cannot authorize a new path.
The live report, policy proof and topology are checked again before PCUpd.

Only a fresh post-synchronization, administratively UP, delegated (`D=1`)
DOWN report with an empty ERO requests an initial path. Its bandwidth and
attributes must satisfy the policy and the supported RSVP profile. A matching
acknowledged UP/ACTIVE report establishes the observed control-plane outcome.
Verify RSVP signalling and forwarding separately.

An eligible healthy UP/ACTIVE delegated LSP with an ERO can be adopted after
reconnection without changing its route. This avoids returning delegation
merely because the listener restarted. It does not authorize active
optimization or establish a global reservation record. New RSVP LSP-IDs for
the same tunnel are distinct from new tunnel ownerships; repeated refusals
must not exhaust history and disconnect unrelated tunnels on the shared PCC
session.

The generic policy does not bypass exclusive creation or managed recovery
owners. A dashboard-owned tunnel covered by an exact managed recovery policy
continues through that policy with its original journal, reservations and
attempt limits. Initial-path eligibility does not grant those same managed
failure/reboot guarantees to every manual tunnel.

Positive bandwidth is required by this implementation. This does not imply
that all RSVP-TE implementations require nonzero reservations. The bandwidth
cap is per LSP, not an aggregate allocation. The LSP cap bounds new admissions
using the PCC's current reported tunnel ownerships, including protected
owners; it does not authorize removing their paths. Advertised TE availability
guides computation, and routers still perform RSVP admission. This profile
does not create shared provisioning reservation-ledger entries for manual
tunnels.

## Diagnose a new tunnel

| Observation | Next check |
|---|---|
| `show pce client peer` has no peer | Global PCC configuration, source routing and controller reachability |
| TCP/session refused by controller | Source prefix, unique current TED node, exact domain membership and collector freshness |
| Session established but not synchronized | Negotiated update capability, PCC reports and synchronization completion |
| DOWN with empty ERO; initial path refused | Sender/source equality, destination membership, positive bandwidth, LSP cap and recorded refusal reason |
| No computed path | Directed TE links, supported constraints and freshness of the BGP-LS feed |
| PCUpd sent without a matching acknowledgement | Delegation, session identity and PCC report/error evidence |
| Path acknowledged but tunnel remains DOWN | RSVP/MPLS-TE configuration and signalling along the returned ERO |
| Tunnel UP but MPLS echo cannot be sent | Interface IP processing, echo source selection and forwarding configuration |

On IOS XE, use `show mpls traffic-eng tunnels tunnel <ID>`,
`show pce client lsp detail`, `show ip rsvp reservation`, MPLS echo and MPLS
trace. Review unrelated unsaved changes before saving a completed router
configuration.

## Trust boundary

BGP-LS domain membership and a matching TCP source are authorization inputs,
not cryptographic PCC authentication. This mode assumes trusted exporters and
a protected routing/control network. A device able to impersonate an approved
router address or inject trusted domain advertisements can undermine that
assumption. Production deployment needs appropriate transport authentication,
exporter protection and access controls.

Discovery removes manual controller registration. It does not qualify all
vendors, remove resource bounds or provide general active optimization,
cross-controller ownership and aggregate admission accounting. The default
listener still permits 16 simultaneous PCC sessions; the computation profile
still bounds its TED to 256 nodes and 2,048 directed links.
