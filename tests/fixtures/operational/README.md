# Operational evidence fixtures

Files ending in `.mock.txt` are synthetic CLI dialect examples, never captured
router output or platform qualification evidence.

The two `.live.txt` files were extracted from private console captures of the
CSR1000v IOS XE 16.9.7 six-router lab. They contain the complete individual
`show mpls traffic-eng tunnels` response; unrelated console commands and the
following prompt were removed, and line endings were normalized to LF.

- `iosxe_16.9.7_tunnel100_up.live.txt`: existing manually configured Tunnel100,
  captured on 2026-10-10 during PCEP path qualification. This response shows a
  50 Mbps reservation and the explicit fallback path active. It is evidence of
  that existing tunnel's CLI dialect, not dashboard or CLI creation success.
- `iosxe_16.9.7_created_zero_bandwidth.live.txt`: diagnostic004 on 2026-10-12,
  an auto-tunnel created through PCInitiate. Its operation-specific name was
  replaced consistently with `NINJA_CREATE_00000000000000000000000000000001`.
  The observed header, bandwidth values, RSVP route and signaling sections are
  preserved. The tunnel was operationally up at zero bandwidth despite the
  controller's 1 Mbps request; this is negative reservation evidence.

The fixtures retain private lab addressing, which identifies the numbered ERO
and tunnel endpoints. They include no public interface addresses or credentials.
Tests that modify these captures create deliberately synthetic failure cases;
those mutations are not additional live outcomes.
