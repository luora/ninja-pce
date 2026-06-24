# Junos lab notes

These are starter snippets, not full golden configs. They assume the router already has:

- reachable management SSH/NETCONF if needed
- loopbacks configured
- core interfaces addressed
- OSPF adjacency working
- MPLS and RSVP enabled on core-facing links

Use the generated `*_junos.txt` file from ninja-pce for the RSVP LSP explicit path.
