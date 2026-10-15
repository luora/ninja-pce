# Topology-domain delegation validation

This records the domain-discovery deployment qualified on 2026-10-15
(Africa/Nairobi) in the eight-router, two-area IOS XE 16.9.7 lab. PCEP path
delivery, RSVP reservations and MPLS forwarding were checked separately.

## Problem and resulting behavior

A newly configured manual tunnel previously needed its own initial-delegation
allowlist entry. Another router also needed a static controller headend
registration, even when the trusted BGP-LS feed already described it. Repeated
refusals could fill the per-session update history and disrupt unrelated
tunnels sharing the PCC session.

The deployed generic policy discovers eligible sources and destinations from
the current validated topology domain. It checks exact protocol, instance,
AS and BGP-LS identifier membership, scoped loopback ranges and supported
RSVP constraints. It does not enumerate ordinary tunnel IDs or individual
headends. Domain transit filtering applies even when the source also has a
static binding for a separate legacy recovery profile.

Every router still requires its global PCC peer/reporting configuration once.
Creating an interface with a dynamic PCE path option does not perform that
router setup. The router owns the tunnel interface, and the PCE supplies its
initial ERO through PCUpd.

## Completed checks

| Check | Result |
|---|---|
| Deployment preflight | Fresh validated TED contained eight physical routers and 22 directed links |
| Offline regression suite | 1,773 tests passed in 149.79 seconds |
| Static checks | Ruff and mypy passed |
| PE1 Tunnel10030 | Fresh delegated ACTIVE report; generic domain-policy PCUpd acknowledged |
| Existing PE1 tunnels | Tunnel100, Tunnel1002 and Tunnel1003 remained ACTIVE at the post-activation check |
| Manual initial-path configuration | Tunnel1003's individual allowlist flag removed; neither Tunnel1003 nor Tunnel10030 has a generic per-ID entry |
| Legacy recovery boundary | Exact Tunnel100 binding retained for its separate existing recovery profile |
| Managed recovery boundary | Tunnel1002 retained its separate exact managed operation, reservations and attempt history |
| Activation ownership | Existing managed ownership and recovery history preserved; no operation recreation or counter reset |
| PCC bootstrap | All eight routers established fresh, synchronized PCEP sessions using their TE loopbacks |
| PE4 Tunnel1003 | Generic domain-policy PCUpd acknowledged; ACTIVE with connected RSVP signalling |
| Transit-router headend | P5 originated a temporary tunnel to PE1 with automatic domain-policy path delivery |
| Forwarding | Both requested tunnels and the temporary P5 tunnel passed MPLS echo 5/5 and their MPLS traces |
| Independent reservations | Matching 1 Mbps reservations observed on both transit routers of each requested path |
| Persistence | Reviewed PCC configuration saved on all eight routers; operator-created tunnel configuration saved; persistent controller override and lab-only PCEP firewall scope installed |
| Healthy listener restart | Eight new synchronized sessions retained five ACTIVE LSPs and identical EROs, with no path updates |
| Reconnect ownership | Operation and reservation ledger unchanged; managed failure attempts and startup history unchanged; unrelated service processes unchanged |
| Final dashboard projection | Eight routers, 22 directed links, eight PCC peers, five ACTIVE LSPs and the original single reservation entry |

The TED's eight routers are discovered by their current domain-qualified
loopback identities. Generic discovery does not require eight configured
headend mappings. Existing exact bindings remain for the legacy/managed
profiles described above; their authority does not grant permission to
control a different tunnel.

## Qualified live paths

| Headend / tunnel | Path | Reservation | MPLS echo |
|---|---|---|---|
| PE1 / Tunnel10030 | PE1 → P1 → P2 → P5 | 1 Mbps | 5/5 |
| PE4 / Tunnel1003 | PE4 → P4 → P3 → PE1 | 1 Mbps | 5/5 |
| P5 / temporary Tunnel61000 | P5 → P2 → P1 → PE1 | 1 Mbps | 5/5 |

All three paths had TE metric 30. The temporary P5 tunnel was removed after
qualification and was not saved. This proves that a transit router can
originate a supported LSP without a controller headend or tunnel-ID entry.
The two requested tunnels also passed MPLS echo 5/5 after the listener restart.

The upper path's reservations were independently checked on P1 and P2; the
reverse inter-area path's reservations were checked on P4 and P3. Existing
PE1 Tunnel100, Tunnel1002 and Tunnel1003 remained ACTIVE throughout the final
checks. No router cold reboot was performed in this domain-discovery trial;
saved configuration and healthy controller/session restart were verified.

## Reusable configuration and scope

See the [domain discovery runbook](topology_domain_delegation.md) and
[example policy](../examples/pcep/topology-domain-policy.json). The example
uses invented private address ranges and example domain identities. Live
addresses, transport endpoints, credentials, operation UUIDs, session/SRP
identifiers and private evidence paths are omitted from this record.

This qualification concerns bounded initial path delivery for supported
router-owned RSVP-TE tunnels. Passive adoption of a healthy delegated LSP
does not change its route or create a global reservation entry. Managed
failure/reboot recovery remains a separate exact ownership profile; it is
not automatically granted to every discovered manual tunnel. Active
optimization, all-vendor interoperability, traffic steering, throughput and
cross-controller ownership are outside these checks.

The policy assumes trusted BGP-LS exporters and protected PCC transport.
Topology membership and matching source addresses do not provide
cryptographic PCC authentication.
