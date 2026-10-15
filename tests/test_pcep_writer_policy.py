"""The final path snapshot fence must follow domain/journal authority reads."""
from __future__ import annotations

import asyncio

from test_pcep import _eventually
from test_pcep_delegation import adapter, connect, initial_body

from ninja_pce.pcep import PCRPT, PcepServer, PcepServerConfig, encode_message


def test_topology_change_during_authority_read_prevents_path_write(tmp_path, monkeypatch):
    computer, delegated = adapter(tmp_path, monkeypatch, "127.0.0.1")
    original_validate = delegated.validate_reply
    state = {"topology_current": True, "authority_checked": False}

    def authority(peer, report, outcome):
        state.update(topology_current=False, authority_checked=True)
        return True

    monkeypatch.setattr(delegated, "validate_authorization", authority)
    monkeypatch.setattr(delegated, "validate_reply", lambda reply: state["topology_current"] and
                        original_validate(reply))

    async def run():
        server = PcepServer(PcepServerConfig(("127.0.0.1",), port=0, keepalive=1, deadtimer=5),
                            path_computer=computer, delegated_computer=delegated)
        await server.start()
        reader, writer = await connect(server)
        try:
            writer.write(encode_message(PCRPT, initial_body()))
            await writer.drain()
            await _eventually(lambda: state["authority_checked"])
            assert server.snapshot()["peers"]["127.0.0.1"]["updates"] == {}
            assert not server._connections["127.0.0.1"].owned_plsps
        finally:
            await server.close()
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())
