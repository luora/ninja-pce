# Domain tunnel provisioning

Dashboard provisioning can discover headends and destinations from the fresh,
trusted BGP-LS domain. Every uniquely identified router inside the configured
endpoint scopes is selectable, including transit P routers. Device inventory
does not grant topology membership or remove routers from the selector. A new
router needs a valid domain identity and transport enrollment for writes; it
does not need a new dashboard router or tunnel-number allowlist.

The [capability report and dialog](tunnel_capabilities.md) explain current global
and per-headend prerequisites and manual next steps. Reporting is informational;
backend admission revalidates the exact reviewed plan. Production ownership and
cross-controller fencing remain independently unverified.

The qualified creation path remains Cisco IOS XE 16.9.7 configuration over SSH,
followed by initial PCEP control and independent headend verification. Other
renderers and the shared policy boundary do not establish NETCONF or other
vendor interoperability.

## Runtime policy

Use [the domain provisioning example](../deploy/examples/provisioning-domain.json)
as a starting point. `domain_policy` names an explicit version 1 domain JSON
document, using the same identity and endpoint scopes as
[the delegation policy example](../deploy/examples/domain-policy.json). The
provisioning process loads it separately from collector observations, runtime
configuration, and mutable state. The policy file cannot alias those inputs or
outputs or reside inside `state_dir`.

In this mode omit the top-level `headends` and `destinations` fields. The live
domain supplies them. `computer.node_names` may provide display bindings and
`computer.headends` may retain exact legacy recovery bindings; neither mapping
is a domain membership allowlist. An optional explicit OSPF area policy remains
necessary for a collector feed that uses the multi-area projection.

`principal_policy: "authenticated"` authorizes ordinary creation by every
authenticated proxy principal and requires no `operators` list. The Unix socket
peer, authenticated proxy identity, exact HTTPS origin, secure session, CSRF,
ownership checks, and principal-scoped operation visibility still apply. Do not
expose the socket through a proxy that accepts caller-supplied identity headers.
The exact principal is retained with each operation and idempotency key.

Provisioning bounds are explicit:

| Field | Meaning | Supported bound |
| --- | --- | --- |
| `max_outstanding` | Operations retaining resources at once | 1–256 |
| `tunnel_id_min`, `tunnel_id_max` | Inclusive allocation range within each headend | 1–65535 |
| `max_operations` | Durable operation history limit | 1–4096 |
| `max_previews` | Current reviewed-plan limit | 1–256 |
| `max_journal_bytes` | Maximum journal size | Up to 16 MiB |

The example deliberately configures these limits. Select limits that fit the
deployment's verified capacity and retention needs. Reaching a history bound
refuses new work rather than deleting ownership or idempotency evidence. A
preview reserves no bandwidth; admission rechecks the current directional
reservation ledger before configuration delivery. Tunnel numbers belong to the
headend namespace, while generated configuration names and operation owners
remain globally distinct.

## Transport enrollment

`inventory` supplies management profiles, keyed by `topology_node_id`:

```yaml
devices:
  - name: transit-management
    topology_node_id: transit-a
    host: 192.0.2.40
    port: 22
    device_type: cisco_ios
    username: network-service
    password_env: NINJA_TRANSIT_A_PASSWORD
```

The topology TE loopback determines PCC identity. `host` and `port` determine the
management endpoint and may be completely different addresses. `password_env`
is a credential reference; no password value enters the policy digest or the
operation record. Distinct topology nodes cannot enroll the same management
host and port, since they require independent tunnel namespaces. Each enrolled
endpoint also needs an independently verified
public key in `known_hosts`. Do not obtain trust by accepting an unknown key
during provisioning. Exact OpenSSH host entries, comma-separated exact aliases,
and hashed entries are supported. A nondefault port requires `[host]:port`.
Wildcard, revoked, and certificate-authority entries do not supply a direct
endpoint pin.

Readiness reports missing enrollment, missing credentials, unavailable or
changed host identity, unsupported transport profile, and PCC synchronization
separately from domain eligibility. An unavailable enrollment does not hide the
router. The reviewed plan binds the enrolled profile and pinned key fingerprints;
admission and delivery must still match that transport revision. A credential
value may rotate under its existing reference without rewriting ownership.

Inventory is optional when operating an unenrolled domain for discovery. In that
case all managed nodes remain visible and configuration writes are unavailable
until enrolled. `known_hosts` must be a bounded regular file and cannot be a
symlink. The SSH adapter independently enforces strict host-key checking during
the connection.

## Durable authority and qualification

Policy revisions belong to individual operations. Changing the current domain,
transport pins, bandwidth limit, or allocation range must not reset old owners,
uncertain delivery, held resources, recovery history, or attempt limits. Existing
state requires the [explicit migration and verification procedure](dashboard_provisioning_deployment.md#migrate-an-existing-provisioning-journal);
retain a baseline and backup before changing a running deployment. The two-phase
`python -m ninja_pce.provisioning_migration plan` / `apply` workflow validates
legacy authority, binds the exact original state, and archives it before adding
per-operation policy revisions. It clears old previews and preserves holds,
recovery files, ownership, uncertainty and attempt histories. Healthy legacy
undelegated tunnels remain outside new write authority.

Creation still requires preview, exact plan approval, current policy and
ownership admission, durable intent, configuration delivery, initial PCEP
control, and independent headend verification. A report acknowledgement, ACTIVE
PCEP state, RSVP verification, and a forwarding test are distinct observations.
Delivery or cleanup uncertainty retains the reservation hold.

The [offline validation checkpoint](domain_tunnel_provisioning_validation.md)
records 2,038 passing tests, 88% coverage, static checks, and the clean 0.6.0
release from `31c998d`. Its separate live record qualifies concurrent creation
from two authenticated principals/headends, retries, policy and transport
refusal, service restart preservation, RSVP and forwarding. All eight enrolled
transports were SSH-authenticated and version-qualified. Two qualification
tunnels and their holds remain in place; current `max_outstanding` is 12, with
three retained held services consuming three slots.
Live checks used HTTPS API requests and asset inspection. Interactive browser
sessions, cold VM/router reboots and every possible path remain outside that
qualification. The SSH profile changes running configuration without saving
startup configuration, and managed recovery remains the original-operation
profile. Independently qualify those conditions for each new deployment.
