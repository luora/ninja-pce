# Configuration provisioning deployment

The [capability-driven dialog](tunnel_capabilities.md) uses the existing options
route and security boundary. Upgrade the API and packaged UI together to expose
current prerequisites. Installing these changes grants no new write authority
or production ownership qualification.

These are sanitized deployment examples for the separate dashboard
[configuration provisioning profile](dashboard_tunnel_creation.md#configuration-based-provisioning-profile).
The shipped JSON has `qualified: false`, and the environment example disables
writes. Installing these files does not qualify a device or enable public
creation. Keep the new Nginx provisioning routes absent until the exact profile has
passed its private live qualification.

The [0.6.0 domain validation record](domain_tunnel_provisioning_validation.md)
documents completed concurrent authenticated creation, transport refusal,
service restart preservation and RSVP/forwarding checks. The two qualification
tunnels and holds remain in place. The qualified SSH backend changes running
configuration without saving startup configuration; cold VM/router reboots,
interactive live browser operation and every possible path remain outside that
recorded scope. New deployments require their own reviewed enrollment and
qualification.

## Private runtime policy and SSH

The legacy examples use Ubuntu controller paths and a six-router address plan.
Change paths, topology mappings and the HTTPS origin locally. That explicit
profile requires its inventory SSH host to identify the authorized PCC loopback
in `computer.headends`.

For domain-wide creation, use [the domain runtime example](../deploy/examples/provisioning-domain.json)
and [domain provisioning policy](domain_tunnel_provisioning.md). Set an explicit
`domain_policy` path and `principal_policy: "authenticated"`; omit the top-level
`headends`, `destinations` and `operators` lists. Fresh trusted domain membership
supplies selectable headends and destinations, including transit routers.
Inventory supplies enrolled transports only. Its SSH `host` may differ from the
TE loopback and must have its own independently pinned key; a TE router ID never
implicitly supplies management connectivity or cryptographic identity.

For an explicitly qualified OSPF multi-area feed, add the JSON path
`computer.ospf_multi_area_policy` to the private runtime policy. The file must
contain the same validated ABR identity policy used by the BGP-LS collector and
the PCEP listener's `--compute-ospf-multi-area-policy` option. It must be distinct
from runtime inputs and outputs and outside the mutable `state_dir`. The default
configuration accepts only the original single-area feed.

Provisioning requires the collector, API computation and listener capture to
agree on the exact area policy document and digest. Existing legacy journals
require explicit migration before adopting versioned domain provisioning.
Each migrated or new operation retains its original authorization, owner and
policy revision; later policy changes do not rewrite that authority. The area
option alone does not expand provisioning scope or qualify writes. Keep
`qualified: false` until the exact deployment is tested.

| Example | Controller destination |
|---|---|
| [Runtime JSON](../gns3/controller/ubuntu-24.04/provisioning.json.example) | `/etc/ninja-pce/provisioning.json` |
| [Domain runtime JSON](../deploy/examples/provisioning-domain.json) | Alternative `/etc/ninja-pce/provisioning.json` |
| [Domain scope](../deploy/examples/domain-policy.json) | `/etc/ninja-pce/domain-policy.json` |
| [IOS XE inventory](../gns3/controller/ubuntu-24.04/provisioning-inventory.yml.example) | `/etc/ninja-pce/provisioning-inventory.yml` |
| [Environment variables](../gns3/controller/ubuntu-24.04/provisioning.env.example) | `/etc/ninja-pce/provisioning.env` |
| [Service](../gns3/controller/ubuntu-24.04/ninja-provisioning.service) | `/etc/systemd/system/ninja-provisioning.service` |

Create the private policy files and persistent shared state directory:

```bash
sudo install -d -m 0755 /etc/ninja-pce
sudo install -d -o ubuntu -g ubuntu -m 0700 /home/ubuntu/ninja-pce/build/state
sudo install -o ubuntu -g ubuntu -m 0600 gns3/controller/ubuntu-24.04/provisioning.json.example /etc/ninja-pce/provisioning.json
sudo install -o ubuntu -g ubuntu -m 0600 gns3/controller/ubuntu-24.04/provisioning-inventory.yml.example /etc/ninja-pce/provisioning-inventory.yml
sudo install -o root -g root -m 0600 gns3/controller/ubuntu-24.04/provisioning.env.example /etc/ninja-pce/provisioning.env
```

Set `NINJA_PCE_PE1_PASSWORD` privately in the root-owned environment file.
The example deliberately leaves it empty; the delivery adapter rejects a
missing credential. The inventory contains only the environment variable name.
Do not place the password in JSON, YAML, a unit's command line or Git. The
existing `southbound` optional dependency must be installed in the reviewed
controller virtual environment.

For the observed IOS XE 16.9.7 lab image, install from the reviewed repository
checkout with the explicit
[compatibility constraints](../gns3/controller/ubuntu-24.04/provisioning-constraints.txt):

```bash
/home/ubuntu/ninja-pce/.venv/bin/python -m pip install -c gns3/controller/ubuntu-24.04/provisioning-constraints.txt -e '.[southbound]'
```

That image offers only SHA-1 SSH key exchange and RSA signatures, which
[Paramiko 5 removed](https://www.paramiko.org/changelog.html#5.0.0).
The constraint applies to this isolated lab profile; qualify a modern router
image before changing its SSH dependency profile. Host-key pinning remains
mandatory.

Install a regular, independently pinned OpenSSH `known_hosts` file at
`/etc/ninja-pce/known_hosts`, readable by the service. Confirm each router key
against an independent console observation before accepting it. A network key
scan alone does not establish identity. The runtime refuses a missing or
symlinked file and uses Netmiko's strict host-key checking with only this
specified file; it does not automatically trust an unknown or changed key.

In the legacy allowlist profile, the operator username in JSON is the HTTPS Basic-auth principal, while the
inventory username is the separate router SSH account. They are different
identities and need not have the same name.

## Service and PCEP ownership bridge

Run the API as the controller user, with the prepared Unix socket group:

```bash
sudo install -m 0644 gns3/controller/ubuntu-24.04/ninja-provisioning.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ninja-provisioning
systemctl status ninja-provisioning --no-pager
journalctl -u ninja-provisioning -n 50 --no-pager
```

The unit runs:

```text
/home/ubuntu/ninja-pce/.venv/bin/python -m ninja_pce.cli_provisioning --config /etc/ninja-pce/provisioning.json
```

For a packaged deployment, point the reviewed service override at the release's
installed interpreter, use `python -I -m ninja_pce.cli_provisioning`, and set
`UnsetEnvironment=PYTHONPATH PYTHONHOME`. Verify `ninja_pce.__version__` and
`ninja_pce.__file__` with that isolated interpreter before activation. Apply the
same environment isolation to the controller service so its child processes
inherit the reviewed installed environment; see the
[activation check](deployment.md#validate-and-activate).

It loads the secret through `EnvironmentFile`, creates
`/run/ninja-provisioning` as `ubuntu:www-data` with mode0750, and the API creates
`http.sock` with mode0660. The runtime directory is removed at shutdown;
operation journals and reservation holds remain in persistent state. The
example trusts proxy UID33, the usual Ubuntu `www-data` UID. Verify that actual
Nginx workers use that exact UID and group before deployment. Do not broaden
socket permissions to make a different proxy identity work. These ownership
settings use systemd's
[runtime directory support](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#RuntimeDirectory=).

Append this flag to the existing reviewed PCEP listener's complete command:

```text
--provisioning-state /home/ubuntu/ninja-pce/build/state/cli-provisioning.json
```

Preserve its existing computation, Tunnel100 delegation and failure-recovery
arguments. Both services must run matching reviewed code, read the same PCEP
capture, and share the exact journal path. The backend remains unavailable if
the listener does not advertise the matching provisioning policy, or PCC/TED
freshness and synchronization are unavailable. This journal permits initial
control of the exact owned new tunnel; it does not widen Tunnel100's recovery
allowlist to all managed IDs. All delivery processes must use the same durable
reservation ledger directory.

## Migrate an existing provisioning journal

Keep the original private runtime configuration and inventory. Do not edit the
only copy into the new domain policy: the migration validates the legacy
journal's original policy digest against those original inputs. Prepare the
new domain runtime configuration separately and retain its exact state paths.

Use a maintenance window with provisioning and recovery writers quiesced. Take
a fresh baseline of tunnels, owners, holds, failure/startup attempts and pending
uncertainty. Create a private review plan; these paths are synthetic examples:

```bash
python -m ninja_pce.provisioning_migration plan \
  --legacy-config /private/provisioning-legacy.json \
  --config /private/provisioning-domain.json \
  --plan /private/migration-plan.json \
  --preserve /private/recovery/managed.json
```

Repeat `--preserve` for every recovery or reinstatement history JSON that must
remain unchanged. The checksummed plan binds the original journal, exact
reservation ledger, policy inputs and each preserved file. Inspect it locally
alongside the original journal and ledger, confirming original ownership,
authorizations, statuses and held resources.
Then apply that reviewed plan to a new private backup directory outside mutable
provisioning state:

```bash
python -m ninja_pce.provisioning_migration apply \
  --plan /private/migration-plan.json \
  --backup-dir /private/migration-backup
```

Apply rechecks all bound hashes, archives the exact original files and manifest,
then atomically writes the version 2 provisioning journal. It adds policy
history and each operation's original `policy_revision`, and explicitly clears
old previews. It preserves operation authorization, principal, owner, tunnel
identity, status, uncertainty, holds, recovery history and attempt limits.
The reservation ledger and preserved recovery files are not rewritten.
Plan and backup files remain private; do not commit them.

Start matching reviewed listener and provisioning code only after migration.
Recheck the baseline, fresh domain/PCC readiness and authenticated operation
visibility before allowing new creation. New domain operations may run within
the explicit concurrency limit; the existing exact managed recovery binding
remains separate and retains its historical budget.

## Recovery of one provisioned tunnel

Managed recovery is an explicit qualification profile for one configuration
provisioning operation. Enable it only after that operation is `VERIFIED` with
independent headend RSVP and PCEP control evidence. Append these arguments to
the complete reviewed PCEP command:

```text
--provisioning-state /home/ubuntu/ninja-pce/build/state/cli-provisioning.json
--managed-failover-operation <VERIFIED_OPERATION_ID>
--managed-failover-state /home/ubuntu/ninja-pce/build/managed-recovery/journal.json
```

Replace `<VERIFIED_OPERATION_ID>` with the exact 32-character lowercase
hexadecimal operation ID from the creation journal. Both managed recovery flags
are required. Its journal must have a separate parent directory from the shared
provisioning state and remain distinct from live inputs, captures and the legacy
recovery journal. Retain Tunnel100's existing delegation, `--failover-tunnel`
and `--failover-state` arguments and its persisted attempt count; the two
profiles must target different tunnels. Do not add the managed tunnel to the
blanket `--compute-delegated-tunnel` allowlist.

The listener requires the original operation, unchanged ownership and
constraints, its exact shared reservation hold, fresh synchronized PCC reports,
and a matching fresh topology policy. It adopts a healthy delegated path before
monitoring that path for a stable link failure. Defaults permit at most three
persisted recovery sends, with a five-second debounce, a thirty-second cooldown
and a thirty-second acknowledgement timeout. It does not optimize healthy paths
or switch back automatically when a failed link returns.

Before sending a replacement, the planner adds its edges to the original
owner's held resources. Old and replacement edges remain held as a union;
recovery does not refund or release reservations. A pending operation found on
restart blocks recovery for explicit reconciliation and is never replayed.
Ownership changes or uncertain results also retain holds and require review;
do not clear journals or reset attempt counts to retry.

Set `NINJA_PCE_PREVIEW_ONLY=1` in the listener's environment to suppress recovery
writes. Inspect `managed_failure_recovery` in its capture independently of the
legacy `failure_recovery` status. Qualify this exact single-tunnel profile with
recorded forwarding and controlled failure evidence before making a broader
deployment claim; these arguments alone do not establish live qualification.

### Explicit reinstatement after the active path is lost

A managed tunnel can remain DOWN with an empty ERO after a controller restart
or an externally changed configuration. A previously verified creation record
does not authorize replaying the original delivery. First restore the tunnel's
exact authorized destination, bandwidth and priorities, then inspect the
managed recovery journal. Resolve any pending or uncertain write separately;
the reinstatement command refuses a pending operation.

Configure one private reinstatement request path in the complete reviewed PCEP
listener command, alongside its existing managed recovery arguments:

```text
--managed-reinstate-request /home/ubuntu/ninja-pce/build/managed-recovery/reinstate-request.json
```

The local two-phase command uses the same private provisioning runtime policy
as the dashboard backend. Create a short-lived plan while the listener and PCC
are fresh and synchronized:

```bash
python -m ninja_pce.pcep_reinstate plan \
  --config /etc/ninja-pce/provisioning.json \
  --operation <VERIFIED_OPERATION_ID> \
  --recovery-state /home/ubuntu/ninja-pce/build/managed-recovery/journal.json \
  --capture /home/ubuntu/ninja-pce/build/live/pcep.json \
  --request /home/ubuntu/ninja-pce/build/managed-recovery/reinstate-request.json \
  --plan /home/ubuntu/ninja-pce/build/managed-recovery/reinstate-plan.json
```

Inspect the mode0600 plan locally. It binds the original operation and retained
resources, recovery journal revision and attempt count, the current PCC session
and tunnel report, and the computed strict path. Planning writes only the review
artifact. The default review interval is sixty seconds; if it expires, create a
new plan with a new artifact name and review the fresh evidence.
The tunnel report must also remain within the computation profile's report-age
limit (thirty seconds by default). An older report is refused even when the
review artifact has not expired. No request is published by a refused check.

Authorize the reviewed plan with the same arguments:

```bash
python -m ninja_pce.pcep_reinstate authorize \
  --config /etc/ninja-pce/provisioning.json \
  --operation <VERIFIED_OPERATION_ID> \
  --recovery-state /home/ubuntu/ninja-pce/build/managed-recovery/journal.json \
  --capture /home/ubuntu/ninja-pce/build/live/pcep.json \
  --request /home/ubuntu/ninja-pce/build/managed-recovery/reinstate-request.json \
  --plan /home/ubuntu/ninja-pce/build/managed-recovery/reinstate-plan.json
```

Authorization rereads the live capture, immutable ownership, held resources,
recovery journal and candidate path before atomically publishing the private
request. Existing plan and request files are never overwritten. The listener
checks the request again against its live session and fresh TED immediately
before sending. A changed report, expired plan, changed authority, exhausted
attempt budget or competing capacity hold prevents delivery.

Reinstatement consumes one attempt from the existing recovery budget and
preserves all prior reservation holds and the immutable creation operation.
The request is one-shot; an unresolved pending intent on restart remains blocked
and is never replayed. A matching correlated UP or ACTIVE report establishes the
new monitored baseline. Inspect the recovery journal's consumed-request record
before archiving any artifact or attempting another explicit review. Do not
delete recovery history or reset the attempt count to retry.

`NINJA_PCE_PREVIEW_ONLY=1` prevents authorization and listener writes. This
workflow changes neither authentication nor firewall policy and adds no public
API route. Keep request and review artifacts private and out of Git.

### Automatic managed startup recovery

For a verified tunnel that must return after its PCC reconnects, enable this
separate opt-in alongside the exact existing managed operation and journal:

```text
--managed-startup-recovery
```

The immutable VERIFIED operation remains the source of the tunnel's identity,
destination, bandwidth and priorities. The listener waits for a synchronized,
live, administratively enabled, delegated DOWN report with an empty ERO and a
fresh usable TED. Historical synchronization reports cannot trigger a write.
PCEP does not distinguish a router reboot from a session reconnect; this policy
covers both under the same exact tunnel profile.

An exact post-synchronization report with delegation clear or administration
disabled invalidates live authority and waits; it does not permanently block a
later exact delegation. Removal likewise invalidates the observed path. A
changed configured profile or immutable owner still blocks, and withdrawal
with an outstanding pending update retains that uncertainty without replay.

Startup delivery has its own persistent budget: one send per PCEP session, at
most three sends in a rolling hour, with at least thirty seconds between sends.
Its total attempt count is monotonic. Enabling this policy does not reset or
consume the separate lifetime failure-recovery count. Rate-limited sessions
remain eligible when capacity in the rolling window returns; an uncertain
pending write or ownership/constraint mismatch still requires reconciliation.

The policy uses the session owner's current observation, so an unchanged live
delegation can wait for IGP/BGP-LS convergence beyond the local maintenance
command's thirty-second report-age limit. Every send still revalidates the
current PCC session, report, exact ownership, held resources and fresh graph.
Old captures cannot grant startup authority. A topology refresh before durable
intent discards the stale proposal and recomputes; any enlarged conservative
hold is persisted first. Deferral spends no attempt and sends no message.
Failure to persist that state stops recovery.

Before sending `STARTUP_PATH`, the listener retains the union of existing and
candidate resources and durably records the startup attempt and pending intent.
A correlated processing acknowledgment alone is insufficient: the matching
UP/ACTIVE path report must complete the operation. A pending startup write found
on controller restart remains blocked and is never replayed.

Inspect `managed_failure_recovery.startup_recovery` for policy, total attempts,
current rate-window timestamps and recent attempt records. Disabling the flag
stops new automatic startup deliveries while preserving that history. Keep
Tunnel100's existing delegation and failure-recovery arguments unchanged.

## Authenticated HTTPS proxy

Keep the existing TLS certificate, HTTPS Basic authentication, static dashboard
routes and default404 route. Add a separate operator entry to the existing
password file without replacing viewer credentials:

```bash
sudo htpasswd /etc/nginx/ninja-dashboard/htpasswd operator
```

The legacy example API allowlist is `operators: ["operator"]`; its existing
`dashboard` viewer receives 403 from the provisioning API. Domain mode uses
`principal_policy: "authenticated"` with no `operators` list, so every account
authenticated by this proxy can create within deployment policy. Preserve
existing proxy accounts and manage authentication at that boundary.
`GET /api/tunnel-options` issues the principal-bound session cookie and CSRF
token; there is no separate session route. The API records the actual principal,
scopes operations and idempotency keys to it, and accepts no external role header.

After qualification, render the
[proxy snippet](../gns3/controller/ubuntu-24.04/nginx/ninja-provisioning-proxy.conf.template)
using the exact HTTPS origin's host and optional port. For the sanitized example:

```bash
DASHBOARD_HOST=controller.example.test envsubst '${DASHBOARD_HOST}' < gns3/controller/ubuntu-24.04/nginx/ninja-provisioning-proxy.conf.template > /tmp/ninja-provisioning-proxy.conf
sudo install -m 0644 /tmp/ninja-provisioning-proxy.conf /etc/nginx/snippets/ninja-provisioning-proxy.conf
sudo install -m 0644 gns3/controller/ubuntu-24.04/nginx/ninja-provisioning-locations.conf /etc/nginx/snippets/ninja-provisioning-locations.conf
```

Restrict `envsubst` to that one variable; Nginx's own `$remote_user` and header
variables must survive rendering. Include the locations **inside the existing
authenticated HTTPS server**, before its general regex location:

```nginx
include /etc/nginx/snippets/ninja-provisioning-locations.conf;
```

The [locations snippet](../gns3/controller/ubuntu-24.04/nginx/ninja-provisioning-locations.conf)
permits only GET options/operation status and POST plans/tunnels. Exact options
matching takes precedence over the existing read-only dashboard route. No
teardown, generic CLI, wildcard API forwarding or alternate TCP backend is
added. The proxy overwrites `X-Ninja-Principal` from authenticated `$remote_user`,
removes caller role and Basic-auth headers, fixes Host to the configured origin,
and preserves the caller's Origin, CSRF token and cookie. It leaves the API's
Secure/HttpOnly/SameSite session cookie intact. See Nginx's
[proxy configuration reference](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header).

Validate with `sudo nginx -t` before reloading. Confirm an unauthenticated
HTTPS request still receives 401. In the legacy profile a viewer outside the
allowlist receives 403; in domain mode each authenticated account can fetch
its own options/session and operation state. POST without matching Origin,
session and CSRF remains forbidden in both modes. Qualify multiple principals
and headends with concurrent independent operations, same-key retries, policy
refusal, missing/changed transport identity, restart preservation and independent
RSVP/forwarding checks before enabling the domain deployment. Keep the JSON
unqualified and writes disabled until recorded qualification permits enabling
the exact profile. Native PCInitiate remains separately unqualified.
