# Ninja-PCE user guide

Ninja-PCE learns a traffic-engineering topology from BGP-LS, computes constrained
RSVP-TE paths, and supplies those paths to routers over PCEP. The topology-domain
policy lets any eligible router in the managed domain act as a PCC. Ordinary
router-owned tunnels need no individual router or tunnel-ID registration in the
controller.

Use the [deployment guide](deployment.md) to install the application. This guide
explains what to configure on the network, what the dashboard means, and how to
operate the available tunnel workflows.

## Start and inspect the application

The application reads one versioned controller JSON file. Its supervisor runs
the BGP-LS collector, PCEP listener and read-only dashboard together. The GoBGP
speaker remains a separate service. A qualified SSH provisioning service and
its authenticated HTTPS proxy also remain separate.

For the packaged systemd installation:

```bash
sudo -u ninja-pce /opt/ninja-pce/releases/0.6.0/venv/bin/ninja-pce controller validate \
  --config /etc/ninja-pce/controller.json
sudo systemctl start ninja-controller
systemctl status ninja-controller --no-pager
journalctl -u ninja-controller --no-pager -n 80
sudo -u ninja-pce /opt/ninja-pce/releases/0.6.0/venv/bin/ninja-pce controller status \
  --config /etc/ninja-pce/controller.json
```

`validate` checks configuration without starting listeners or sending router
commands. `plan` shows the component commands for review. `status` inspects the
configured observations; it does not bring a stopped component back online.
It returns exit code `0` only when the configured supervisor processes are
running, topology and PCEP sources are fresh, and at least one PCC is freshly
synchronized. Exit code `1` means degraded, including the expected bootstrap
state before any PCC has connected. It always reports forwarding as unverified.
The configuration digest includes the domain/ABR policy, node map and protobuf
content. After changing these inputs, review the new plan and restart the
supervisor; status reports a mismatch while the old configuration is running.

For an interactive installation, run the same validated configuration in the
foreground:

```bash
ninja-pce controller plan --config /path/to/controller.json
ninja-pce controller run --config /path/to/controller.json
```

Stop it with Ctrl-C. To run selected components while independently managing
the others, repeat `--component`, for example
`--component bgpls --component dashboard`. Keep exactly one PCEP listener and
one writer for each collector output set. Do not start the foreground supervisor
alongside a systemd instance using the same addresses and files.

If one selected child exits unexpectedly, the supervisor stops the remaining
children and reports failure. The installed systemd service then restarts the
group; use its logs to diagnose repeated restarts.

By default the dashboard is at <http://127.0.0.1:8088>. Connect through an
existing SSH tunnel or the separately configured HTTPS reverse proxy. The
loopback HTTP server has no built-in login or TLS.

## Choose a tunnel workflow

| Workflow | Who creates the tunnel interface? | Controller authority |
|---|---|---|
| Router-owned tunnel | The router operator or existing automation | Initial path delivery for an eligible, delegated LSP; passive adoption of a healthy LSP after reconnect |
| Dashboard configuration provisioning | A separately qualified SSH adapter, after an operator reviews a preview | Exact generated configuration and initial PCEP path for the recorded operation |
| Managed failure/startup recovery | The previously recorded provisioning operation already owns the interface | Separate, explicitly enabled recovery for that exact operation, with retained reservations and attempt limits |

A router being eligible as a PCC does not grant the dashboard SSH access to it.
A generic initial path does not register a manual tunnel as a dashboard-owned
operation or grant it the managed recovery profile. Native bandwidth-aware RSVP
creation using PCInitiate is not qualified on the tested IOS XE image; use one
of the two configuration workflows above.

## Prepare the network

The controller is an endpoint with routed connectivity to the SP domain. It
does not need to carry customer traffic or form an IGP adjacency. Keep the
control interface and Internet/default-route interface distinct when using a
two-interface host.

Before starting tunnel tests:

1. Establish the IGP, numbered IPv4 core links and loopback reachability.
2. Enable MPLS traffic engineering and RSVP bandwidth on every core interface
   that a tunnel may use. Include every required OSPF area in TE distribution.
3. Export the domain's node and directed TE-link information through BGP-LS.
   The exporter must send the link metrics, administrative groups and bandwidth
   attributes needed for constrained path computation.
4. Configure the controller's domain policy from the actual BGP-LS protocol,
   instance, AS and BGP-LS identifier. Choose PCC and destination prefixes that
   cover the intended TE router IDs.
5. Provide a route from every intended PCC to the controller and a return route
   to the PCC loopbacks. Limit PCEP transport to the protected control network.

For OSPF multi-area deployments, the BGP-LS receiver must retain each area's
node advertisements. Configure the same independently reviewed ABR identity
policy in the collector, computation service and dashboard. The separate
[multi-area guide](multi_area_lab.md) explains the currently qualified GoBGP
identity correction. A reachable destination loopback alone does not prove that
the controller has a complete usable TE path to it.

## Configure a router as a PCC once

The TCP source must match that router's unique, current advertised IPv4 TE
router ID. The domain policy checks the source identity, topology domain and
address scope. Display names and PE/P roles do not determine eligibility.

The following IOS XE 16.9.7 fragment is an example. Replace the placeholders
with that router's real routed addresses:

```text
mpls traffic-eng pcc peer <PCE_ADDRESS> source <TE_ROUTER_ID> delegate-only
mpls traffic-eng pcc report-all
```

Check the session:

```text
show pce client peer detail
show pce client lsp detail
```

A tunnel's `dynamic pce address` setting does not create the global PCC peer.
The peer must connect, negotiate stateful updates and complete synchronization.
Repeat this bootstrap when adding a router to the network; there is no matching
per-router registration step in the controller's domain-discovery profile.

The syntax above is the tested IOS XE profile. Other vendors and releases need
their own PCC configuration and independent interoperability qualification.

## Create a router-owned tunnel

With PCC bootstrap complete, configure a supported tunnel on the chosen
headend. A transit router can be a headend too. The example requests 1 Mbps:

```text
interface Tunnel12345
 description EXAMPLE-RSVP-SERVICE
 ip unnumbered Loopback0
 tunnel mode mpls traffic-eng
 tunnel destination <DESTINATION_TE_ROUTER_ID>
 tunnel mpls traffic-eng priority 7 7
 tunnel mpls traffic-eng bandwidth 1000
 tunnel mpls traffic-eng affinity 0x0 mask 0x0
 tunnel mpls traffic-eng path-option 1 dynamic pce address <PCE_ADDRESS>
 no shutdown
```

Use a free interface number on the router and the actual TE loopback interface.
The sender must match the PCC source. Cisco's tunnel bandwidth command uses
kilobits/s, so `1000` requests 1,000,000 bits/s. The application's domain-policy
bandwidth cap uses bits/s.

The new tunnel needs no controller flag for its interface number. When the PCC
reports a fresh, administratively UP, delegated (`D=1`) DOWN LSP with an empty
intended route, the controller:

1. Checks the current session, exact wire identity, domain membership,
   destination scope, supported attributes, bandwidth and concurrent LSP limit.
2. Preserves the authority of any existing creation or managed recovery owner.
3. Computes a path from the fresh directional TED and rechecks the evidence
   before sending a PCUpd with strict numbered IPv4 hops.
4. Records the router's correlated acknowledgement and later operational state.

The router performs RSVP signalling and resource admission. If constraints
cannot be met, inspect the refusal reason and current advertised resources.
The generic profile requires positive bandwidth and does not preempt other
LSPs or grant credit for an existing reservation.

## Verify the result

Treat path computation, protocol processing, RSVP signalling and forwarding as
separate checks. On the headend:

```text
show mpls traffic-eng tunnels tunnel 12345
show pce client lsp detail
show ip rsvp sender
show ip rsvp reservation
```

Confirm the correct destination and bandwidth, Admin/Oper UP, valid path,
connected signalling and the expected current ERO. Check RSVP sender and
reservation state on the selected transit routers. Then run the router's MPLS
echo and trace commands for that tunnel and compare the observed path.

`ip unnumbered` gives the tunnel IPv4 processing in this IOS XE example; a tunnel
can appear UP while an incorrectly configured echo source cannot send a probe.
Do not interpret an unsent probe as evidence of packet loss along the LSP.

Creating a tunnel does not enable autoroute, install customer routes into it or
prove service throughput. Configure traffic steering separately under the
network's normal change process. Review other unsaved router changes before
saving a successful configuration.

## Read the dashboard

The dashboard combines the normalized topology, BGP-LS health, PCEP observations
and local reservation journal. It does not obtain independent forwarding
measurements from every router.

| Display | Meaning |
|---|---|
| Fresh source | Current observation passed its configured health/freshness checks |
| Stale, unavailable or invalid source | Current state is unknown or cannot be trusted for new path delivery |
| Established PCC | The PCEP session is connected; synchronization is a separate condition |
| Synchronized PCC | The listener observed completion of that session's synchronization |
| ACTIVE LSP | The PCC currently reports an active LSP; verify RSVP and forwarding separately |
| Acknowledged update | A correlated report confirms processing; it may still report DOWN or GOING-UP |
| Local reservation | A controller operation holds capacity in the shared journal; this is distinct from a router's RSVP reservation |
| Not advertised node | A previously displayed node is absent from the current feed; its retained position does not authorize computation |

Topology positions stay stable when tunnel overlays change. Link labels combine
the two directional utilization values; the direction/order is shown by the
link's details. The percentages reflect the advertised TE bandwidth information,
not sampled traffic-counter utilization.

Manual domain-policy tunnels do not create entries in the shared provisioning
reservation ledger. Their RSVP use is reflected through the live advertised
bandwidth as the network updates its TED.

## Create a tunnel from the dashboard

This workflow is available only when the separate configuration provisioning
backend has been qualified and exposed through an authenticated HTTPS proxy.
The normal dashboard server is read-only. Domain discovery does not enable
router SSH writes or add provisioning inventory automatically.

1. Open the authenticated dashboard and choose **Create tunnel**.
2. Select a managed headend, a permitted destination and the requested
   bandwidth in decimal Mbps. In domain provisioning mode, transit routers are
   selectable too. Each headend reports transport and PCC readiness.
3. Review the computed route, actual RSVP bandwidth, generated configuration
   and preview expiry. Whole-kilobit rounding is disclosed in the preview.
4. Click **Create tunnel** once to approve that exact plan.
5. Track the recorded operation and its verification result.

Previewing sends no router configuration and holds no bandwidth. Changing a
field invalidates the preview. Creation rechecks policy, topology, ownership,
capacity and the approved plan before writing through the pinned SSH adapter.
PCEP then supplies the operation's initial path.

| Operation state | Operator action |
|---|---|
| `REJECTED` | Read the definitive prewrite refusal; correct the input or availability and review a new preview |
| `PLANNED` / `PUSHING` | Follow the existing operation; do not start a duplicate |
| `APPLIED_UNVERIFIED` | Inspect the router and verification evidence before treating the service as established |
| `VERIFIED` | The qualified backend verified the headend's identity, destination, bandwidth, ERO, RSVP signalling and initial PCEP control |
| `VERIFICATION_FAILED` / `ROLLBACK_REQUIRED` | Preserve the journal and reservation; reconcile exact router state before cleanup or further writes |
| `ROLLED_BACK` | Recorded cleanup completed and the operation's matching hold was released |

The UI label **RSVP verified on the headend** describes the scope of `VERIFIED`.
It does not prove end-to-end forwarding, transit reservations or throughput.
After a browser/network error, recover the same pending operation using its
retained intent, approved plan and idempotency key. Reload does not resend
automatically. Changing the key while the result is uncertain can create a
second request and must not be used as a retry strategy.

The opt-in [domain provisioning policy](domain_tunnel_provisioning.md) discovers
eligible routers from the fresh trusted topology and authorizes every
authenticated proxy principal within deployment policy. Transport enrollment
supplies a separate management endpoint, credential reference and pinned SSH
identity. Missing enrollment leaves the domain router visible with a readiness
reason. Explicit concurrency and history bounds permit multiple independent
operations; tunnel IDs are allocated within each headend's namespace and
idempotency is scoped to the authenticated principal. Each operation retains
its original policy revision across later policy changes.

The [0.6.0 live qualification](domain_tunnel_provisioning_validation.md)
demonstrated concurrent creation by two principals/headends, scoped retries,
transport/policy refusal, service restart preservation and independent
RSVP/forwarding. The recorded deployment's `max_outstanding` is 12, with three
retained held services consuming three slots; current
path admission and bandwidth policy still apply. Its two qualification tunnels
and holds remain in place. The SSH profile changes running configuration and
does not save startup configuration. Cold VM/router reboot and a live
interactive browser session were outside the recorded qualification.

The legacy explicit inventory profile retains its one-held-tunnel default.
There is no public deletion endpoint or arbitrary router-command endpoint. Use
the [provisioning deployment guide](dashboard_provisioning_deployment.md) for
transport, host-key, qualification, state migration and Unix proxy setup.

## Recovery and restarts

For an ordinary domain-policy tunnel, a healthy delegated UP/ACTIVE report with
an ERO can be adopted after a PCEP reconnect without changing its path. A fresh
eligible DOWN/empty report can request initial computation. This is not general
active reoptimization, guaranteed restoration of every manual tunnel or shared
reservation ownership.

One separately owned provisioning operation can use the explicit managed
failure/startup recovery profile. Its original operation, reservation hold,
constraints and attempt history remain authoritative. See the
[managed startup qualification](managed_startup_recovery_validation.md) and
[recovery deployment procedure](dashboard_provisioning_deployment.md#recovery-of-one-provisioned-tunnel).
A pending or uncertain write is retained for explicit reconciliation; restarting
the application does not authorize replay or reset its recovery budget.

Back up and restore all ownership journals and reservation files together. Do
not delete a journal, reset its counters or release a hold merely because a
tunnel disappeared from the dashboard. Confirm current synchronized PCC state,
router configuration and RSVP reservations before any owned cleanup. Reverting
application code does not undo changes already sent to a router.

## Troubleshooting

| Symptom | Check first |
|---|---|
| No PCC peer on a router | Global PCC peer configuration, source address and route to the controller |
| TCP connection refused or times out | Listener address, TCP/4189, control-network firewall and return route |
| Connected source is refused | Fresh unique TE node, TCP-source/TE-router-ID match, exact domain identity and PCC prefix scope |
| Session does not synchronize | Negotiated stateful updates, router reporting configuration and end-of-sync evidence |
| Fresh BGP-LS data but no usable path | Complete directed links, bandwidth/metric attributes, area policy, constraints and destination identity |
| New tunnel stays DOWN with empty ERO | Initial delegation refusal, positive bandwidth, supported attributes, ownership conflict and concurrent LSP cap |
| PCUpd acknowledged but tunnel stays DOWN | Router RSVP errors, strict ERO reachability and RSVP/MPLS-TE on every selected core interface |
| Public dashboard works but Create is disabled | Qualified backend, authenticated principal policy, exact HTTPS origin, per-node transport/PCC readiness, fresh trusted TED, and configured concurrency/history bounds |
| HTTP result is uncertain | Recover the existing operation; retain its key, intent and plan |
| Restart reports an uncertain operation | Preserve holds and inspect current router state; do not replay or clear history |
| Multi-area destination is IP-reachable but missing from TE | Export all relevant area nodes and links; inspect ABR identity policy and receiver node retention |

Inspect the collector and PCEP diagnostics before restarting services. A router's
`Destination IP address ... not found` can describe its local area-limited TED;
it does not by itself prove that the controller lacks the destination.

## Supported scope and trust

The qualified live profile is numbered IPv4 RSVP-TE on IOS XE 16.9.7, including
the recorded two-area lab. The code includes other rendering/parsing candidates;
their presence does not certify PCEP or provisioning interoperability. See
[platform support](platform_support.md) and the
[topology-domain validation](topology_domain_validation.md) for evidence.

The current generic path profile uses strict remote-interface hops, positive
bandwidth, supported priorities/administrative groups and minimum additive TE
metric. IPv6 paths, unnumbered hops, SR-TE, general active reoptimization,
preemption and cross-controller ownership are outside this profile. Default
limits include 16 simultaneous PCC sessions, 256 TED nodes and 2,048 directed
links; the domain policy separately bounds concurrent LSPs per PCC and bandwidth
per LSP. The bandwidth cap is not an aggregate domain allocation.

Domain membership and a matching TCP source provide eligibility inside a
protected network; they do not cryptographically authenticate a router. Protect
the BGP-LS exporters and PCEP transport, restrict the controller's control
interface and local GoBGP API, and add the deployment's transport authentication
boundary before production use. Authenticated PCEPS/TLS is not supplied by this
application. HTTPS protects the dashboard connection and is a separate boundary.
