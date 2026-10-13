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
