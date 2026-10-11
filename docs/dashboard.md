# Controller dashboard

The `feat/controller-dashboard` branch adds a read-only, UniFi-inspired web interface
for topology, BGP-LS/PCEP peers, reported LSPs, computations and recent PCEP events.
It uses the existing controller observations and needs no additional web packages.
Dark and light themes work without external assets or Internet access.

## Try the interface

From the repository root, with the project installed in its virtual environment:

```bash
.venv/bin/ninja-pce dashboard --demo
```

Open <http://127.0.0.1:8088>. A persistent **Demo** banner identifies the synthetic
six-router example. Demo mode never reads or changes live controller files.

## Show the live controller

On the Ubuntu controller, run from the same checkout and working directory used
by the BGP-LS collector and PCEP listener:

```bash
cd ~/ninja-pce
.venv/bin/python -m pip install -e .
.venv/bin/ninja-pce dashboard
```

The default paths are:

| Input | Default path |
|---|---|
| Normalized topology | `build/live/topology.json` |
| BGP-LS collector status | `build/live/bgpls-status.json` |
| BGP-LS captured input | `build/live/bgpls-raw.json` |
| PCEP observations | `build/live/pcep.json` |
| Shared reservation directory | `build/state` |

Use `--topology`, `--bgpls-status`, `--bgpls-raw`, `--pcep` and `--state-dir` to
match the running controller. The dashboard does not start the protocol services.
It works with missing inputs and displays their availability individually.

The listener binds to loopback by default. To reach it from a workstation, forward
the port over an existing SSH connection:

```bash
ssh -L 8088:127.0.0.1:8088 ubuntu@CONTROLLER_ADDRESS
```

Then open <http://127.0.0.1:8088> on the workstation. Alternatively, bind explicitly
to the controller's private lab address, for example:

```bash
.venv/bin/ninja-pce dashboard --host 172.16.100.10 --port 8088
```

The dashboard process has no authentication or TLS. Use SSH forwarding, a trusted
lab network, or the authenticated HTTPS proxy below for public access.

## Interpret the display

- Inputs are checked every five seconds by default. `--refresh` changes browser
  polling; `--max-age` changes the observation freshness limit (default 30 seconds).
  Pausing freezes the view and labels it as paused.
- Freshness comes from observation timestamps and peer expiry. The dashboard
  verifies checksums where present, and matches the normalized topology against
  the corresponding BGP-LS capture and collector status. File modification time
  does not make an old observation current.
- **ACTIVE** is the PCC's reported state, not independent evidence of forwarding.
  Stale reports retain their last reported state but lose their current status.
- Selecting an LSP highlights a route only when its ERO resolves unambiguously to
  directed links in the fresh observed topology. Unresolved routes remain visible as
  addresses rather than a guessed path.
- Router positions stay fixed when tunnels reroute, links change or an LSP is
  selected. The browser remembers positions for each topology and mode across
  page reloads; temporarily absent routers reuse their positions when they return.
  With browser storage unavailable, positions remain stable for the open page.
  Previously observed routers remain visible when their advertisements disappear,
  labelled **Not advertised** in a fresh snapshot. Previously observed connections
  remain as muted dashed outlines, with no current bandwidth values or route
  eligibility. Source failures instead label the retained map **Observation
  unavailable**. Current counts and tables still describe the observed TED.
  Browser history cannot supply routers or links that were never observed; older
  position-only history can recover router identities, but has no link history.
- Link bandwidth shows advertised RSVP reservation data, not measured traffic.
  Local ledger holds are separate controller accounting and can remain after a
  peer observation expires.
- Events and computations show up to 80 retained records. The API limits input
  files to 8 MiB, peers to 64, LSPs to 512, topology nodes to 256 and directed links
  to 2,048. Oversized or malformed inputs show an invalid source state.

The UI offers route inspection and copying. It has no controller mutation,
reoptimization or tunnel teardown actions. The server does not create a ledger or
write observation files. `GET /api/controller` returns the same bounded projection
used by the UI; raw captures, credentials and arbitrary filesystem paths are not
served. `GET /healthz` reports only the dashboard process, not protocol health.

## Optional Ubuntu service

The example unit [ninja-dashboard.service](../gns3/controller/ubuntu-24.04/ninja-dashboard.service)
assumes user `ubuntu` and checkout `/home/ubuntu/ninja-pce`. Adjust these paths if
needed, then install it:

```bash
sudo install -m 0644 gns3/controller/ubuntu-24.04/ninja-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ninja-dashboard
systemctl status ninja-dashboard --no-pager
journalctl -u ninja-dashboard -n 50 --no-pager
```

The unit listens on loopback and reads the default live paths. Use SSH forwarding
or Nginx to inspect it from another machine.

## Public access through Nginx

Keep the dashboard on `127.0.0.1:8088`. Nginx listens on the IPv4 address assigned
to the Internet interface `ens3`: HTTPS on port 443 and an HTTP-to-HTTPS redirect
on port 80. The private lab interface continues carrying BGP-LS/PCEP.

From the checkout containing the dashboard deployment files, install the proxy
prerequisites and run the [setup script](../tools/setup_dashboard_nginx.sh):

```bash
sudo apt-get update
sudo env NEEDRESTART_MODE=l apt-get -o DPkg::Lock::Timeout=300 install nginx apache2-utils gettext-base openssl
sudo bash tools/setup_dashboard_nginx.sh
```

`NEEDRESTART_MODE=l` lists services needing an update restart so the package
installation does not automatically recycle active PCEP sessions.

On a fresh Nginx installation, its stock welcome site can remain enabled. To bind
only the public interface, back up and disable that stock site. Moving from its
wildcard port-80 listener to an address-specific listener can require a one-time
`sudo systemctl restart nginx`; validate the configuration first. Preserve other
sites on an existing Nginx deployment.

The script selects the single global IPv4 address on `ens3` by default and keeps
existing credentials and certificates. It checks the certificate identity and
expiry, validates Nginx, and reloads the proxy. Use `--interface` or `--public-ip`
only when changing the intended interface or selecting its assigned address.
It checks the HTTPS authentication challenge after reloading and restores the
previous site if that check fails.

The [Nginx template](../gns3/controller/ubuntu-24.04/nginx/ninja-dashboard.conf.template)
uses `${PUBLIC_IP}` as its only deployment placeholder. Set that value locally
to the current `ens3` IPv4 address; do not commit the rendered configuration,
public IP, passwords or private keys. When rendering with `envsubst`, restrict
substitution to `'${PUBLIC_IP}'` so Nginx variables remain intact. If the DHCP
address changes, update the bind configuration and certificate identity.

The proxy mounts the dashboard at `/` and allows only `/`, `/index.html`,
`/styles.css`, `/app.js`, `/api/controller` and `/healthz`. Every dashboard route
requires HTTP Basic authentication over TLS. The proxy permits GET/HEAD, disables
caching and removes the authentication header before forwarding to the dashboard.
The API and health check receive the same protection as the page.

Deployment files stay on the Ubuntu controller:

| File | Purpose |
|---|---|
| `/etc/nginx/ninja-dashboard/fullchain.pem` | TLS certificate and chain |
| `/etc/nginx/ninja-dashboard/privkey.pem` | TLS private key; root access only |
| `/etc/nginx/ninja-dashboard/htpasswd` | Password hashes readable by Nginx |
| `/home/ubuntu/.config/ninja-dashboard/initial-password` | Initial credential, available through the controller console |

The bootstrap account is `dashboard`. Retrieve its generated password from the
controller console with `sudo cat ~/.config/ninja-dashboard/initial-password`.
The bootstrap certificate is self-signed, valid for 30 days, and includes the
actual public IP as an IP subject alternative name. Before entering credentials,
verify its SHA-256 fingerprint against the controller console and explicitly trust
that certificate in the browser, or replace it with a publicly trusted certificate:

```bash
sudo openssl x509 -in /etc/nginx/ninja-dashboard/fullchain.pem -noout -sha256 -fingerprint
```

For an isolated dashboard checkout at `/home/ubuntu/ninja-dashboard`, reuse the
existing controller virtual environment while reading its existing live files.
Create this service override with `sudo systemctl edit ninja-dashboard`:

```ini
[Service]
Environment=PYTHONPATH=/home/ubuntu/ninja-dashboard/src
WorkingDirectory=/home/ubuntu/ninja-pce
ExecStart=
ExecStart=/home/ubuntu/ninja-pce/.venv/bin/python -m ninja_pce.cli dashboard --host 127.0.0.1 --port 8088
```

Then run `sudo systemctl daemon-reload` and `sudo systemctl restart ninja-dashboard`.

After installing the proxy configuration, run `sudo nginx -t` before reloading
Nginx. Verify that unauthenticated page/API requests return 401, authenticated
requests succeed, unknown paths return 404, and port 8088 is bound only to
loopback. Keep the previous configuration available for rollback and preserve
the existing BGP-LS/PCEP firewall rules.

A publicly trusted certificate can replace the bootstrap certificate. Let's
Encrypt supports IP certificates through Certbot 5.4 or later using the
`shortlived` profile and a webroot challenge. IP certificates expire after six
days, so configure automatic renewal and a deploy hook that updates the files
above, validates Nginx, and reloads it. The template serves only ACME challenge
files from `/var/lib/ninja-dashboard/acme` without authentication on port 80;
other HTTP requests redirect to HTTPS. Use that directory as the webroot and
follow the [official IP certificate instructions](https://letsencrypt.org/2026/03/11/shorter-certs-certbot.html)
and review the CA terms during setup. Staging certificates are not publicly
trusted. A certificate from another CA can also use the same local paths.

## Validation

The branch passes the repository's 655 tests, Ruff and mypy. The 72 dashboard tests
include historical BGP-LS/PCEP fixture projections, stale and invalid inputs,
retained reservation resources, strict/loose ERO metadata and HTTP boundaries.
Chrome checks cover both themes, keyboard selection, filtering, source/API failure,
recovery, reduced motion, unavailable browser storage and widths from 320 to
1,440 pixels. The wheel includes all three UI assets.

Topology layout checks also confirm unchanged router coordinates across reroutes,
link withdrawal/restoration, selection of another headend, shuffled input order,
unavailable snapshots, router disappearance/reappearance, new routers and page
reloads during a failure. Disabled or malformed browser storage is handled without
preventing the map from rendering.
Retained-map checks cover PE3 withdrawal, browser reload while PE3 is absent,
restoration, position-only cache migration and directional link withdrawal.
Fresh omissions are distinguished from unavailable/stale sources; historical
connections supply neither bandwidth values nor resolved ERO hops.

Browser interaction checks use explicit synthetic demo data and a view with absent
input files. The isolated dashboard service also reads the running GNS3 controller:
six topology nodes, sixteen directed links and one reported active LSP, with fresh
BGP-LS and PCEP sources. The existing protocol service processes were preserved.
The public Nginx deployment passes certificate-pinned external HTTPS checks,
HTTP redirect checks and an external check that port 8088 is unreachable.
Authenticated page, API and asset requests succeed; unauthenticated requests are
challenged, mutation methods are denied, and arbitrary file/traversal routes are
rejected. Re-running setup preserves the certificate and credentials.
