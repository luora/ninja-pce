# TE input schema and compatibility

All rates use decimal bit/s (`1 Mbps = 1,000,000 bit/s`), not bytes/s. The internal
canonical values are integers. Reservations round upward and capacities downward
at sub-bit precision. Cisco renderers round reservations upward to whole kbit/s
and refuse if that rounded value does not fit. Junos renders integer bit/s.

## Directional TED resources

Existing `nodes`, `links`, `reservable_bw_mbps`, `reserved_bw_mbps`, and
`bidirectional: true` inputs remain accepted. Shorthand expands to forward ID
unchanged and `<id>:reverse`, with swapped interface/IP endpoints and `base_id`
preserved. Every internal link is directional. Two physical links need different
IDs and numbered endpoint addresses to preserve strict ERO identity.

```yaml
metadata:
  name: example
  units: Mbps
nodes:
  - {id: A, loopback: 10.255.0.1}
  - {id: B, loopback: 10.255.0.2}
links:
  - id: A-B-1
    from: A
    to: B
    from_ip: 10.0.0.1
    to_ip: 10.0.0.2
    local_interface: GigabitEthernet0/0/0/0
    remote_interface: GigabitEthernet0/0/0/0
    metric: 10
    max_bw_mbps: 1000
    reservable_bw_mbps: 100
    reserved_bw_mbps: 10
    unreserved_bw_mbps: [90, 90, 90, 90, 90, 90, 90, 90]
    admin_up: true
    oper_up: true
    admin_groups: [GREEN]
    srlg: [fiber-a]
    bidirectional: true
    reverse:
      metric: 30
      reservable_bw_mbps: 80
      reserved_bw_mbps: 10
      unreserved_bw_mbps: [70, 70, 70, 70, 70, 70, 70, 70]
```

Use separate `bidirectional: false` entries for explicitly independent directions,
or `reverse` overrides when using shorthand. Explicit reverse values are never
replaced by the forward attributes. Duplicate generated/explicit IDs are rejected
unless they unambiguously specify the intended explicit directional reverse.
Use `max_bw_bps`, `reservable_bw_bps`, `reserved_bw_bps`, `unreserved_bw_bps` for
integer bit/s inputs. Do not mix conflicting representations. Maximum reservable
bandwidth must not exceed maximum link bandwidth; observed reserved bandwidth
must not exceed reservable bandwidth. `timestamp`/`source` on YAML links
retain collector provenance (`Link.provenance_source` internally); metadata supplies snapshot-level authority/freshness.

Legacy aggregate availability converts to the same free value at all eight
priorities. This makes no claim about existing LSP priority distribution.
Explicit vectors must have eight nonnegative, finite, nonincreasing values,
each within reservable capacity. `observed_unreserved_bps(setup_priority)` exposes
advertised admission capacity; nonpreemptive admission additionally bounds it by
priority7, aggregate free capacity, and local pending holds. Higher-priority
advertisement alone never grants permission to preempt. The theoretical deficit
is separately exposed by `preemption_required_bps`; no automatic preemption is
implemented. Router experiments must compare advertised vectors with actual
holding-priority LSPs and observed admission/preemption behavior.

## Demand constraints and ranking

```yaml
service:
  name: example
  source: A
  destination: B
  bandwidth_mbps: 50
  setup_priority: 7
  hold_priority: 7
  path:
    hop_type: strict
    max_candidates: 3
    search_budget: 100000
    avoid_nodes: []
    avoid_links: []
    include_nodes: []
    ordered_waypoints: []
    prefer_lower_metric: true
    prefer_higher_bottleneck: true
  output:
    formats: [generic, iosxr, iosxe, junos]
```

`include_nodes` is unordered; `ordered_waypoints` is an explicit sequence.
Both must be satisfied by one simple directed path, without repeated routers.
`max_hops`, `max_metric`, affinity include-any/include-all/exclude, and directional
link avoidance are supported. Avoiding a shorthand base link ID avoids both
directions; a generated reverse ID can be avoided independently. Unknown nodes,
unknown links, contradictory node requirements and conflicting `path` and
`constraints` entries fail. Source and destination cannot be avoided.
`required_capabilities` optionally requires protocol capabilities on every path
router. Missing declared capability fails safely.

Ranking is lexicographic: enabled TE metric ascending, enabled residual
bottleneck descending, hops ascending, ordered link IDs, then ordered node IDs.
Disabling either preference omits only its ranking term. With both disabled,
fewest hops and stable identity decide. The legacy `score` output remains a
display compatibility field and never affects selection.

The iterative constrained DFS searches all eligible simple edge paths and keeps
the best K. Metric/hop and waypoint prefix pruning are sound. Each examined
eligible outgoing edge consumes one budget unit; default100000. Exhaustion
returns `CspfSearchLimit`, CLI exit3 and **INDETERMINATE / SEARCH_LIMIT**, even
if feasible candidates have been found, because global ranking is unproven.
Only completed exploration can report proven no-path or optimal top K. Time is
exponential in general; this is a correctness-first bounded search, not a claim
of large-network scalability.

## ERO and validation changes

Strict hops carry selected link ID, local address and remote numbered interface
address. Loose hops use router loopbacks and explicitly loose semantics, and
cannot prove the selected physical path. All current vendor renderers support
IPv4 numbered paths only. The core can validate IPv6 addresses, but IPv6 or
unnumbered strict rendering is refused. Duplicate remote addresses on parallel
adjacencies are ambiguous and refused.

Booleans must be YAML/JSON booleans. Quoted `"false"` is rejected, not coerced.
Numeric strings, negative/NaN/infinite bandwidth, invalid priorities/hop types,
duplicate YAML/JSON keys, malformed mappings, unknown schema keys and unsafe CLI
identifiers/control characters fail with actionable errors. Priorities are0–7;
holding priority must be numerically no greater than setup priority. This is the
product's conservative policy following the SHOULD in
[RFC3209 §4.7.3](https://www.rfc-editor.org/rfc/rfc3209.html).
Inventory supports the legacy mapping and canonical device list; inline secrets
are rejected and supported platforms are validated. Host/IP/port and environment
references must be safe. Credentials remain external environment references.

The version2 persisted JSON state and legacy migration restrictions are described
in [reconciliation](reconciliation.md). Input schema changes are additive where
safe; formerly permissive malformed or ambiguous files intentionally fail.
