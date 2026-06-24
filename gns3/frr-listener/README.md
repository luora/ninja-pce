# FRR topology-listener node

This folder is for the optional Phase 2 GNS3 node.

For the first operator demo, ninja-pce can run fully from manual YAML. The FRR listener is added when you want a Linux node to join OSPF/IS-IS for visibility and later export link-state information using BGP-LS.

Important boundaries:

- The FRR node is not an RSVP-TE tunnel head-end.
- The FRR node should not carry production transit traffic.
- Keep interfaces passive unless they must form an IGP adjacency.
- Start with one adjacency in a lab area, validate LSDB visibility, then expand.
- Treat BGP-LS export as Phase 2. The Phase 1 TED is still the YAML file under `/lab/topologies`.

Suggested GNS3 setup:

1. Add an FRR Docker/router node.
2. Connect it to a lab router in the relevant OSPF/IS-IS area.
3. Give it a loopback/router-id.
4. Enable only the FRR daemons needed for the experiment.
5. Validate with `vtysh -c "show ip ospf neighbor"` and LSDB commands.
6. Keep forwarding disabled or isolated from the service path.

The sample `frr.conf.example` is intentionally conservative and should be edited for your GNS3 addressing.
