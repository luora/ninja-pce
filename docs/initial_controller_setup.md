# Initial Ubuntu controller setup

The intended controller learns live TE topology, maintains a directional TED,
computes constrained paths, and controls and verifies RSVP-TE LSPs. P1 is the
selected topology-consumer entry point; PE1 remains the tunnel head-end. The
current code implements file-fed CSPF, shared reservation accounting,
reconciliation and CLI delivery/verification. The branch now also provides live
BGP-LS ingestion and a passive PCEP listener. This initial network procedure
does not start them; continue with [live protocol setup](live_protocol_setup.md).

## Observed Ubuntu state, 2026-10-10

The operator supplied Ubuntu 24.04.4 LTS console output with `systemd-networkd`
active. `ens3` has carrier, DHCP address `192.0.2.81/24`, and default gateway
`192.0.2.254`; Internet ICMP works. That address is an observed lease, not a
static address to configure. DNS still needs a separate check. `ens4` is
administratively down and unconfigured. The only Netplan file is
`/etc/netplan/50-cloud-init.yaml`, defining `ens3` with DHCP4 and DHCP6 and a MAC
match. The commands below are for the **Ubuntu VM console**, not the development
checkout's host; they have not been run on that VM by the remediation tools.

Keep the existing `ens3` definition and cloud-init ownership. The local
[ens4 configuration](../gns3/controller/ubuntu-24.04/60-ninja-pce-lab.yaml) adds a
separate device, with no overlapping address/route lists. This additive change
does not require disabling cloud-init networking. If later datasource changes
also define `ens4`, inspect the merged configuration before applying.

## P1 connection

Cable Ubuntu's second adapter (`ens4`, MAC `02:00:00:00:00:11`) to P1 GbE3.
The router's [base configuration](../gns3/router-configs/ios-xe/16.9.7-six-router/P1.cfg)
already contains this interface. If it has not been applied, use P1's console:

```text
configure terminal
interface GigabitEthernet3
 description TO_pce-ninja_ens4_LINK_P1-PCE-MGMT
 ip address 172.16.100.102 255.255.255.0
 no shutdown
end
show ip interface brief
```

No OSPF adjacency, MPLS or RSVP is needed on the Ubuntu-facing link.

## Ubuntu networking

Back up the current directory outside Netplan's input directory:

```bash
ninja_netplan_backup="/root/netplan-before-ninja-$(date +%Y%m%d-%H%M%S)"
sudo cp -a /etc/netplan "$ninja_netplan_backup"
```

Create the new file with mode 0600. These commands preserve `50-cloud-init.yaml`:

```bash
sudo install -m 600 /dev/null /etc/netplan/60-ninja-pce-lab.yaml
sudo tee /etc/netplan/60-ninja-pce-lab.yaml >/dev/null <<'YAML'
network:
  version: 2
  ethernets:
    ens4:
      renderer: networkd
      match:
        macaddress: "02:00:00:00:00:11"
      set-name: ens4
      dhcp4: false
      dhcp6: false
      accept-ra: false
      optional: true
      addresses:
        - 172.16.100.10/24
      routes:
        - to: 10.255.0.0/24
          via: 172.16.100.102
YAML
sudo netplan generate
sudo netplan get
sudo netplan apply
```

The observed state has no existing local file by this name. If repeating this
procedure after editing that file, retain its previous version in the backup
before replacing it. Direct application is appropriate from the independent
GNS3 console. `netplan generate` validates/generates configuration without
normally applying it. A console operator may instead use
`sudo netplan try --timeout 120` for an interactive trial, verifying live and
on-disk state if the trial is cancelled or times out.

Verify:

```bash
ip -br address
ip -4 route
ip route get 8.8.8.8
ip route get 10.255.0.2
ping -c 3 172.16.100.102
ping -c 3 10.255.0.2
ping -c 3 8.8.8.8
getent hosts github.com
```

Expected routes (DHCP lease may change):

```text
default via 192.0.2.254 dev ens3
10.255.0.0/24 via 172.16.100.102 dev ens4
172.16.100.0/24 dev ens4 src 172.16.100.10
```

P1's loopback `10.255.0.2` is locally reachable on P1 once its base configuration
is applied; the other router loopbacks require the later OSPF/routing stage and
return reachability for `172.16.100.0/24` through P1. A passive OSPF advertisement
of P1's controller subnet can provide the return route without an Ubuntu OSPF
adjacency. Ubuntu remains an endpoint, with no bridge, forwarding or NAT between
its NICs. A failed P1 ping calls for checking P1 Gi3, the cable, MAC mapping and
`ip -br link`; do not replace the working Internet default route.

For recovery from a mistaken new definition, move the new YAML outside
`/etc/netplan`, regenerate and apply from the console, then inspect live routes
and addresses. Retain the original backup. Netplan rollback has caveats; confirm
the on-disk state before rebooting.

## Runtime and reviewed source

After the Internet and DNS checks succeed:

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip ca-certificates
python3 --version
timedatectl status
```

Use the reviewed `fix/te-cspf-reliability` branch. The work in this session has
not been pushed to GitHub. Transfer the supplied `ninja-pce-validation.bundle`
to `/tmp` on Ubuntu using an available file-transfer mechanism. It contains
the committed remediation branch, without the local virtual environment or
uncommitted files. Clone it into a new directory:

```bash
git clone --branch fix/te-cspf-reliability /tmp/ninja-pce-validation.bundle ~/ninja-pce
cd ~/ninja-pce
git log -1 --oneline
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,southbound]'
.venv/bin/python -m pip check
.venv/bin/ninja-pce --help
```

If `~/ninja-pce` already exists, inspect it before selecting an empty clone
destination; do not overwrite it. The bundle clone's origin is a local bundle;
configure a GitHub remote when the reviewed branch is published.

## First offline check

These commands validate file-fed computation and render candidate configuration;
they do not connect to routers:

```bash
cd ~/ninja-pce
.venv/bin/ninja-pce compute \
  --topology examples/topologies/simple_multiarea.yml \
  --demand examples/demands/pe1_to_pe3_50m.yml \
  --output-dir build/controller-smoke --show-candidates
.venv/bin/python gns3/validation/setup_lab.py --output build/gns3-validation
.venv/bin/python gns3/validation/run.py \
  --manifest build/gns3-validation/manifest.json --scope offline \
  --output build/gns3-offline-results.json
```

Expected example result: PE1 → P3 → P4 → PE3, metric 50, demand 50 Mbps.
The example reservations are simulated inputs, not observations from the lab.
The current offline matrix reports 13 PASS and 2 SKIP; these counts do not
establish router interoperability. Initial router qualification and the live
evidence procedure remain in [GNS3 validation](gns3_validation.md).

References: [Netplan YAML](https://netplan.readthedocs.io/en/1.0/netplan-yaml/),
[generation](https://netplan.readthedocs.io/en/1.0/netplan-generate/),
[apply](https://netplan.readthedocs.io/en/1.0/netplan-apply/),
[trial and rollback](https://netplan.readthedocs.io/en/1.0/netplan-try/),
and [cloud-init networking](https://docs.cloud-init.io/en/latest/reference/network-config.html).
