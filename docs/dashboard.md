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

This first version has no authentication or TLS. Keep it on a trusted lab network
or use SSH forwarding; the controller's Internet-facing interface is not the
intended bind address.

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
  directed links in the displayed topology. Unresolved routes remain visible as
  addresses rather than a guessed path.
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
to inspect it from another machine.

## Validation

The branch passes the repository's 655 tests, Ruff and mypy. The 72 dashboard tests
include historical BGP-LS/PCEP fixture projections, stale and invalid inputs,
retained reservation resources, strict/loose ERO metadata and HTTP boundaries.
Chrome checks cover both themes, keyboard selection, filtering, source/API failure,
recovery, reduced motion, unavailable browser storage and widths from 320 to
1,440 pixels. The wheel includes all three UI assets.

Browser checks use explicit synthetic demo data and a live view with absent input
files. This dashboard has not yet been exercised against the running GNS3
controller in this branch.
