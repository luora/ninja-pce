# IOS XR lab notes

These are starter snippets, not full golden configs. They assume the router image already has:

- reachable management SSH
- loopbacks configured
- core interfaces addressed
- OSPF adjacency working
- MPLS and RSVP enabled on core-facing links

Use the generated `*_iosxr.txt` file from ninja-pce for the tunnel explicit path. Review the generated change plan before applying.
