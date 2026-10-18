# Authenticated observation setup

PR 4 adds an opt-in local administration service for a new observer. It stays
available before network configuration and while protocol processes fail,
activate or stop. Its guided forms generate validated controller, GoBGP,
node-label and optional multi-area identity files. Existing installations keep
their current configuration and ownership workflow.

## Start a new workspace

From a reviewed checkout:

~~~bash
./ninja-pce admin init --directory .ninja-pce/admin-workspace
./ninja-pce admin serve --directory .ninja-pce/admin-workspace \
  --grpcurl /path/to/reviewed/grpcurl --gobgpd /path/to/qualified/gobgpd
~~~

The explicit init command prints a one-time enrollment token, valid for thirty
minutes. Keep it private, open http://127.0.0.1:8080/setup, and use it to create
the sole local administrator with a password of 15 to 128 characters. The
token is consumed once the account is committed. An unclaimed expired token
can be rotated locally:

~~~bash
./ninja-pce admin bootstrap-token --directory .ninja-pce/admin-workspace
~~~

The server never prints the token, accepts it in a URL or exposes its hash.
The browser clears submitted credentials and uses no local/session storage.
Restarting management invalidates every session. Keep the account password in
your password manager; account recovery and multi-user administration are
outside this first-launch profile.

The directory must be new; init refuses an existing or partially initialized
workspace. Every created directory has mode0700 and administration files have
mode0600. Run subsequent commands under the same OS account. Setup owns only
this workspace's new observation outputs and revisions. It neither discovers
nor imports PROD/DEV configuration, credentials or journals.

Omit --gobgpd only when deliberately using an independently managed GoBGP API
at 127.0.0.1:50051. In that case, generated gobgpd.toml is a review artifact;
the GUI manages only its collector. The installed binary paths come from the
local command or service unit and cannot be changed by a browser request.

For remote access, keep the server on loopback and use SSH forwarding:

~~~bash
ssh -L 8080:127.0.0.1:8080 <HOST_ACCOUNT>@<CONTROLLER_ADDRESS>
~~~

Open the exact http://127.0.0.1:8080/setup URL on the workstation. This service
supports loopback HTTP only; public binding, HTTP proxy identity headers and
arbitrary browser origins are rejected. It is a distinct endpoint from the
existing read-only dashboard and separately qualified provisioning proxy.

## Review and activate

1. Replace documentation addresses with the controller's real router ID,
   source address, local ASN and intended BGP-LS peer addresses/ASNs. The source
   address must already exist on a configured interface. Setup does not change
   host networking, router configurations or firewalls.
2. Review BGP listening: -1 disables inbound BGP, while a positive port enables
   the explicit source-address listener. Outbound peer sessions remain possible
   with inbound listening disabled. Only the BGP-LS family is configured, with
   global and neighbor export rejected.
3. Set the OSPFv2 domain tuple, optional node labels, and any independently
   approved ABR bindings. Every binding explicitly identifies its physical
   node name, TE router ID, IGP router ID and area memberships. It must match a
   node-label entry and pass the existing strict multi-area validator.
4. Preview generated files. Editing any form invalidates that preview. Save
   commits the reviewed configuration without starting protocol work. Save
   and activate also starts the service's owned observer processes. A running
   observer requires activation when changing configuration, to keep its
   runtime aligned with the saved revision.
5. Check runtime and network readiness separately. Local process startup does
   not prove an established BGP session, End-of-RIB or a complete fresh graph.
   An unavailable network source keeps management available. Open dashboard
   to inspect the existing observation views under the same authenticated
   session; tunnel provisioning stays disabled.

Diagnostics offers a snapshot refresh and explicit live read-only checks with
stable codes, evidence and manual next steps. Management remains responsive
while these checks wait. See the [diagnostics guide](diagnostics.md) for coverage,
API boundaries and limits.

Preview responses bind a random five-minute preview ID to the administrator,
expected current revision and exact generated contents. Apply compares that
revision again. A conflict requires reloading the saved settings and reviewing
a new preview. Generated paths are fixed by the server. Forms accept no
filesystem paths, command arguments, credentials, provisioning state or PCEP
write authority. This profile creates observe mode only.

Single-area collection retains the existing parser's validation. The domain
tuple becomes an enforced projection scope when explicit ABR bindings are
configured; a label or matching address alone never creates an ABR identity.
The model supports uint64 instance IDs; the browser explicitly limits editing
to exact JavaScript integers through 9,007,199,254,740,991 and refuses unsafe
persisted values rather than rounding them.

## Revisions, failures and recovery

Revisions live below workspace/revisions/<content-digest>. Each includes the
typed setup, generated artifacts and a checksummed file inventory. Every use
checks the inventory, exact bytes, private file ownership and the existing
controller validator. The active revision is a checksummed regular manifest,
updated with atomic replacement and filesystem synchronization.

Activation records APPLYING before switching configuration or stopping owned
processes. It verifies native API ownership and local collector startup; it
does not wait for topology availability. A failed activation restores the
previous revision and restarts its previously running observer when cleanup
can be confirmed. Rollback retains observation, reservation, provisioning and
recovery files. Errors and revision changes appear in the latest 256 durable
administration audit entries. Generated revisions are capped at 256 per
workspace; archiving requires a deliberate local maintenance procedure.

Unknown shutdown or a management crash leaves UNCERTAIN instead of claiming
that lost process handles prove a stopped observer. Further mutations are
blocked. Stop management and inspect the retained runtime records and logs.
After the intended writers have been stopped through their normal controls,
run local reconciliation with the same executable selections:

~~~bash
ninja-pce admin reconcile --directory /private/observer-workspace \
  --grpcurl /path/to/reviewed/grpcurl --gobgpd /path/to/qualified/gobgpd
~~~

Reconciliation refuses a live component record, held supervisor/collector
lock or occupied managed API/BGP listener. It restores the prior configuration
and leaves the observer stopped. It never kills a PID discovered in a record
or listener snapshot. The collector holds its own writer lock, covering the
crash window before its supervisor persists the child's identity. Restart
management, sign in again and start the reviewed saved observer deliberately.

A clean management stop journals and reaps its own children. A normal restart
preserves configuration and opens management with the observer stopped. Keep
the same reviewed executable selections for a workspace; changing release
paths requires an explicit migration review. Existing active PCEP deployments
must retain their original state paths and separate migration procedure.

## Verified bundle service

The offline installer also renders ninja-admin.service. It uses the installed
isolated interpreter and qualified native binaries, with a separate
/var/lib/ninja-pce/admin-workspace by default. Installation initializes no
administrator and starts/enables no service. On a fresh observer host, choose
this service as the owner of collection and GoBGP instead of starting the
separate ninja-controller and ninja-gobgpd units against the same ports.

Initialize under the configured service account, using the installed release:

~~~bash
sudo -u ninja-pce /opt/ninja-pce/releases/0.7.0/venv/bin/python -I -m ninja_pce.cli \
  admin init --directory /var/lib/ninja-pce/admin-workspace
sudo systemctl enable --now ninja-admin
~~~

Use the actual rendered paths/account if they differ. The unit restricts
writes to the data directory, supplies only CAP_NET_BIND_SERVICE for BGP,
bounds other restarts and prevents retries for permanent startup exit78.
Its ninety-second stop window covers activation and owned-child shutdown.
Management has no dependency that tears it down when a protocol child fails.
Do not activate competing units or migrate a working installation implicitly.

## Authentication and acceptance scope

Password storage uses salted scrypt at N=2^17, r=8, p=1 with a 256MiB memory
ceiling, following the [OWASP password-storage recommendation](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#scrypt).
Authentication attempts share a five-per-minute limit within management.
There are at most sixteen sessions, with fifteen-minute idle and eight-hour
absolute expiry. Host-only cookies are HttpOnly and SameSite=Strict. Secure
cookies require TLS; this profile is restricted to loopback HTTP/SSH transport.

Every mutation, including login and bootstrap, requires the exact configured
Origin; authenticated mutations additionally require a session-specific CSRF
token. This follows OWASP's advice to use [CSRF defenses alongside session
cookies](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html#samesite-attribute).
Duplicate security headers/cookies and JSON keys, nonfinite JSON, oversized
bodies and transfer encoding are rejected. Requests have bounded concurrency
and read deadlines. Credentials, tokens and cookie values are excluded from
request logging and configuration/audit responses. Local accounts with access
to the service account remain inside the host trust boundary.

Tests use real local HTTP, browser assets, disposable child processes and
private workspaces. They exercise initial login, preview/save/activation,
conflicts, interrupted recovery, lock ownership, markup-safe rendering and
management availability while the topology source is unavailable. Packaging
checks cover installed assets and generated systemd units. Live router and
disconnected fresh-VM activation qualification remain separate release work.
