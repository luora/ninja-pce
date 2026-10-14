# Configuration provisioning deployment

These are sanitized deployment examples for the separate dashboard
[configuration provisioning profile](dashboard_tunnel_creation.md#configuration-based-provisioning-profile).
The shipped JSON has `qualified: false`, and the environment example disables
writes. Installing these files does not qualify a device or enable public
creation. Keep the new Nginx operator routes absent until the exact profile has
passed its private live qualification.

## Private runtime policy and SSH

The examples use the existing Ubuntu controller paths and private six-router
address plan. Change paths, topology mappings and the HTTPS origin locally.
The headend inventory's SSH host must also identify the authorized PCC loopback
in `computer.headends`; do not substitute the console-server endpoint.

For an explicitly qualified OSPF multi-area feed, add the JSON path
`computer.ospf_multi_area_policy` to the private runtime policy. The file must
contain the same validated ABR identity policy used by the BGP-LS collector and
the PCEP listener's `--compute-ospf-multi-area-policy` option. It must be distinct
from runtime inputs and outputs and outside the mutable `state_dir`. The default
configuration accepts only the original single-area feed.

Provisioning requires the collector, API computation and listener capture to
agree on the exact policy document and digest. Changing the identity policy
requires explicit reconciliation of the provisioning journal. This option
does not qualify writes or expand the configured headends, destinations or
bandwidth limit; keep `qualified: false` until that exact deployment is tested.

| Example | Controller destination |
|---|---|
| [Runtime JSON](../gns3/controller/ubuntu-24.04/provisioning.json.example) | `/etc/ninja-pce/provisioning.json` |
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

The operator username in JSON is the HTTPS Basic-auth principal, while the
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

## Authenticated HTTPS proxy

Keep the existing TLS certificate, HTTPS Basic authentication, static dashboard
routes and default404 route. Add a separate operator entry to the existing
password file without replacing viewer credentials:

```bash
sudo htpasswd /etc/nginx/ninja-dashboard/htpasswd operator
```

The example API allowlist is `operators: ["operator"]`. The existing `dashboard`
viewer is not authorized for the operator API. `GET /api/tunnel-options` issues
the principal-bound session cookie and CSRF token; there is no separate session
route. The API authorizes exact principal names, not an externally supplied role
header.

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
HTTPS request still receives401, the viewer receives403 from operator routes,
the authorized operator can fetch options/session state, and POST without
matching Origin, session and CSRF remains forbidden. Keep the JSON unqualified
and writes disabled until recorded qualification permits enabling this exact
profile. Native PCInitiate remains separately unqualified.
