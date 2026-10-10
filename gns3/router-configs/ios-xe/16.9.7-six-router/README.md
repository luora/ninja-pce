# Six-router IOS XE base configuration

These are console-paste configuration candidates for the operator's six-router
GNS3 layout and IOS XE 16.9.7 image. They establish hostnames, interface
descriptions, loopback addresses and directly connected link addresses. They
have not been applied to routers. The files are intended for the initial lab
baseline, rather than merging into a previously configured network.

The image's GNS3 `GbE1`–`GbE4` labels are assumed to correspond to IOS
`GigabitEthernet1`–`GigabitEthernet4`. Provision four Ethernet adapters per
router for these files, including currently unused ports. A router with only
two occupied ports can still use Gi4; reducing its adapter count would remove
or renumber that port. Check `show ip interface brief` and the
actual cables through each GNS3 console before pasting. Preserve the current
configuration/project snapshot first. Apply the file matching the device, then
verify its state before saving with `copy running-config startup-config`.
No authentication or console-access changes are included.

| Router | Loopback0 /32 | Gi1 | Gi2 | Gi3 | Gi4 |
|---|---|---|---|---|---|
| PE1 | 10.255.0.1 | P1 Gi1; 10.0.12.1/29 | P3 Gi2; 10.0.13.1/29 | Unused, shutdown | Reserved parallel to P3 Gi1, shutdown |
| P1 | 10.255.0.2 | PE1 Gi1; 10.0.12.2/29 | P2 Gi2; 10.0.25.2/29 | Ubuntu ens4; 172.16.100.102/24 | P3 Gi4; 10.0.23.2/29 |
| P3 | 10.255.0.3 | Reserved parallel to PE1 Gi4, shutdown | PE1 Gi2; 10.0.13.3/29 | P4 Gi3; 10.0.34.3/29 | P1 Gi4; 10.0.23.3/29 |
| P4 | 10.255.0.4 | P2 Gi1; 10.0.54.4/29 | PE3 Gi2; 10.0.46.4/29 | P3 Gi3; 10.0.34.4/29 | Unused, shutdown |
| P2 | 10.255.0.5 | P4 Gi1; 10.0.54.5/29 | P1 Gi2; 10.0.25.5/29 | Unused, shutdown | PE3 Gi4; 10.0.56.5/29 |
| PE3 | 10.255.0.6 | Unused, shutdown | P4 Gi2; 10.0.46.6/29 | Unused, shutdown | P2 Gi4; 10.0.56.6/29 |

All eight core links use mask `255.255.255.248`. Ubuntu `ens4` uses
`172.16.100.10/24`, with its Internet/default route still on `ens3`. The
optional parallel core link remains unconfigured and administratively down.

The files contain only the addressing baseline. OSPF, MPLS, RSVP-TE, LSPs and
live topology export are subsequent stages. At this point, directly connected
neighbors can communicate; remote loopbacks are not expected to be reachable.
No background RSVP reservations have been established by these files.

After applying the base files, inspect each router:

```text
show running-config | include hostname
show ip interface brief
show interfaces description
show ip route connected
```

Ping the directly connected neighbors:

| Router | Neighbor addresses |
|---|---|
| PE1 | 10.0.12.2, 10.0.13.3 |
| P1 | 10.0.12.1, 10.0.25.5, 10.0.23.3, 172.16.100.10 |
| P3 | 10.0.13.1, 10.0.34.4, 10.0.23.2 |
| P4 | 10.0.54.5, 10.0.46.6, 10.0.34.3 |
| P2 | 10.0.54.4, 10.0.25.2, 10.0.56.6 |
| PE3 | 10.0.46.4, 10.0.56.5 |

For example, on PE1 run `ping 10.0.12.2` and `ping 10.0.13.3`. Core ports should
be up/up when their peer is configured and running. Unused/reserved ports should
be administratively down. P1 Gi3 should be up/up after Ubuntu is connected and
addressed. Save only after confirming the intended interface mapping.

Base interface syntax reference: [Cisco IOS XE Fuji 16.9 virtual interfaces](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/interface/configuration/xe-16-9/ir-xe-16-9-book/ir-cfg-vir-if-xe.html).
Protocol qualification and the later test sequence are in
[GNS3 validation](../../../../docs/gns3_validation.md).
