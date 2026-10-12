"""A stalled browser page must not hold up generation, Stop or saves."""
import asyncio
import time

import pytest

from local_agent import agents
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.store import Store


class Socket:
    def __init__(self, stall=False, fail=False, stall_close=False):
        self.stall, self.fail, self.stall_close = stall, fail, stall_close
        self.sent, self.closed = [], []

    async def send_json(self, data):
        if self.fail:
            raise RuntimeError("connection lost")
        if self.stall:
            await asyncio.Event().wait()  # never returns
        self.sent.append(data)

    async def close(self, code=1000):
        if self.stall_close:
            await asyncio.Event().wait()
        self.closed.append(code)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "SOCKET_SEND_TIMEOUT", 0.05)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    store = Store(settings.state_dir / "sockets.sqlite3")
    yield AgentManager(store, settings)
    store.db.close()


async def test_a_stalled_socket_is_dropped_and_the_others_still_receive(manager):
    stalled, healthy, failing = Socket(stall=True), Socket(), Socket(fail=True)
    manager.listeners["chat"] = {stalled, healthy, failing}

    started = time.monotonic()
    await asyncio.wait_for(manager.broadcast("chat", {"type": "status", "status": "running"}), 2)
    assert time.monotonic() - started < 1

    assert healthy.sent == [{"type": "status", "status": "running"}]
    assert manager.listeners["chat"] == {healthy}
    await asyncio.sleep(0.01)
    assert stalled.closed == [1011] and failing.closed == [1011]  # the page reconnects for a fresh snapshot

    await manager.broadcast("chat", {"type": "status", "status": "idle"})
    assert healthy.sent[-1] == {"type": "status", "status": "idle"}
    assert stalled.sent == []


async def test_closing_a_stalled_socket_is_bounded_and_does_not_block_broadcast(manager):
    stuck = Socket(stall=True, stall_close=True)
    manager.listeners["chat"] = {stuck}
    started = time.monotonic()
    await asyncio.wait_for(manager.broadcast("chat", {"type": "delta"}), 2)
    assert time.monotonic() - started < 1 and not manager.listeners["chat"]
    await asyncio.sleep(0.2)
    assert not manager.closing_sockets  # the background close gave up after its deadline
