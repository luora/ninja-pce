# Initial paths for manually configured tunnels

A router interface does not automatically grant Ninja-PCE permission to control
it. A manually configured tunnel needs an explicit PCC/tunnel-ID binding for
initial PCEP path computation. Dashboard provisioning grants separate authority
through its owned operation journal.

## Reusable onboarding steps

1. Configure the RSVP-TE underlay and a reachable, stateful PCC/PCE session.
2. Configure the tunnel with a positive reservation bandwidth and the PCE path
   option. For the qualified IOS XE IP/MPLS profile, borrow a routed loopback
   address with `ip unnumbered`.
3. Add the tunnel's exact PCC/IP and interface number to the listener's
   repeatable initial-path option. Preserve its current computation inputs,
   headend mapping and existing recovery arguments.
4. Reload the reviewed service configuration and restart the PCEP listener.
   Wait for fresh post-synchronization delegation; an archived report or D=0
   cannot authorize a path update.
5. Verify the acknowledged PCUpd, matching ACTIVE route, RSVP reservations and
   MPLS forwarding independently. Save the router configuration after reviewing
   unrelated unsaved changes.

Controller options use these bindings:

```text
--compute-headend PCC_IP=HEADEND_LOOPBACK
--compute-delegated-tunnel PCC_IP=TUNNEL_ID
```

The second option is repeatable. It authorizes the PCC/tunnel ID, not an
immutable destination and bandwidth profile. Use reviewed bindings for this
limited initial-path mode; it does not register a manually created interface
as a dashboard-owned managed operation.

Router fragment; replace the placeholders and retain the existing global
PCC, OSPF, MPLS-TE and RSVP configuration:

```text
interface Tunnel<TUNNEL_ID>
 ip unnumbered <LOOPBACK_INTERFACE>
 tunnel mode mpls traffic-eng
 tunnel destination <DESTINATION_LOOPBACK>
 tunnel mpls traffic-eng priority 7 7
 tunnel mpls traffic-eng bandwidth <POSITIVE_KBPS>
 tunnel mpls traffic-eng path-option 1 dynamic pce address <PCE_ADDRESS>
```

Zero or missing requested bandwidth is rejected by the current local
computation profile. This is an implementation restriction, not a statement
that RSVP-TE generally requires a positive reservation.

## Qualified PE1-to-P5 example

On 2026-10-14 UTC, an operator-created Tunnel1003 to P5 was administratively UP
but operationally DOWN with an empty ERO. The initial pasted output requested
zero bandwidth; by live inspection the router already requested 1000 kbps.
The PCEP session was established, but Tunnel1003 had no owned provisioning
record and was absent from the initial-path allowlist.

The pre-change policy authorized initial computation for PE1 Tunnel100, with
separate managed recovery for the previously verified Tunnel1002. A read-only
preview of Tunnel1003 against that policy returned:

```text
Initial delegation has no exact, current configured-tunnel authorization
```

The live configuration added only this initial-path binding:

```text
--compute-delegated-tunnel 10.255.0.1=1003
```

The listener then supplied the path through PCUpd. PE1 reported working
delegation, RSVP signalling connected and a matching ACTIVE LSP. Default MPLS
echo initially returned `Q` (request not sent); its interface had IP processing
disabled by `no ip address`. Explicitly choosing the echo source also returned
`Q`. Applying `ip unnumbered Loopback0`, as used by the existing working
tunnels, enabled the default echo and trace.

| Check | Observation |
|---|---|
| Platform | CSR1000v IOS XE 16.9.7 |
| Tunnel / endpoints | Tunnel1003; PE1 `10.255.0.1` to P5 `10.255.0.7` |
| Requested / signalled bandwidth | 1,000,000 bit/s / RSVP 1000 kbps |
| Path / TE metric | PE1 → P1 → P2 → P5 / 30 |
| Strict ERO | `10.0.12.2`, `10.0.25.5`, `10.0.57.7` |
| PCEP delivery | One acknowledged initial PCUpd; matching ACTIVE ERO |
| Forwarding | Default MPLS echo 5/5; trace matched all three strict hops |
| Independent RSVP | P1, P2 and P5 each showed the same 1 Mbps reservation |
| Persistence | Tunnel1003 saved with Loopback0 unnumbered addressing, 1000 kbps and the existing dynamic PCE option |
| Existing tunnels | Tunnel100 and Tunnel1002 remained up at final verification |
| Existing controller state | Original operation and ledger bytes, managed recovery history and legacy attempt count unchanged |
| Dashboard | Eight nodes, 22 directed links, three active LSPs |

Only the PCEP service restarted. Dashboard, provisioning, collectors and Nginx
processes were preserved. The controller source remained the previously tested
`92a772b` implementation; this trial required no source change. Private captures,
configuration snapshots and console evidence remain outside Git. Public IPs,
credentials, MACs and live operation/session identifiers are omitted here.

## Scope

This qualifies initial path delivery and MPLS OAM for this manual 1 Mbps
IOS XE tunnel. It does not qualify throughput, customer traffic steering,
zero-bandwidth requests, active optimization, other vendors or repeated
failure/reboot recovery for Tunnel1003. Autoroute remained disabled.

Tunnel1003 has no owned entry in the shared provisioning reservation ledger.
The initial-path adapter uses the advertised TED; the router performs RSVP
admission. Tunnel1002 retains its separate exact managed startup/failure policy.
Dashboard configuration provisioning still permits one held managed tunnel;
onboarding a second fully managed operation requires extending that profile,
not creating a journal entry or resetting the existing owner's history.
