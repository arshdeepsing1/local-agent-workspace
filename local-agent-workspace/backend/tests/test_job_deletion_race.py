import asyncio
import os

import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings


@pytest.mark.asyncio
async def test_session_deletion_rejects_new_commands_and_turns_during_job_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://fake.example", "synthetic-test-token")
    app = create_app(settings)
    manager = app.state.manager
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop = manager.jobs.stop

    async def gated_stop(job_id):
        entered.set()
        await release.wait()
        return await original_stop(job_id)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            token = (await client.get("/api/bootstrap")).json()["token"]
            client.headers["X-Local-Token"] = token
            session_id = (await client.post("/api/sessions")).json()["id"]
            query = {"session_id": session_id}
            launched = await client.post("/api/jobs", params=query, json={
                "command": "printf '%s\\n' $$; exec sleep 30", "background": True})
            assert launched.status_code == 200
            job_id = launched.json()["id"]
            async with asyncio.timeout(2):
                while not manager.jobs.get(job_id)["output"]:
                    await asyncio.sleep(0.01)
            process_id = int(manager.jobs.get(job_id)["output"].strip())
            monkeypatch.setattr(manager.jobs, "stop", gated_stop)
            deletion = asyncio.create_task(client.delete(f"/api/sessions/{session_id}"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert not deletion.done()
                command, message, legacy = await asyncio.gather(
                    client.post("/api/jobs", params=query, json={"command": "sleep 30", "background": True}),
                    client.post(f"/api/sessions/{session_id}/messages", json={"text": "new turn"}),
                    client.post("/api/command", params=query, json={"text": "sleep 30"}),
                )
                assert [command.status_code, message.status_code, legacy.status_code] == [404, 404, 404]
            finally:
                release.set()
                response = await asyncio.wait_for(deletion, 3)
            assert response.status_code == 200
            assert manager.jobs.list(session_id=session_id) == []
            assert manager.get(session_id) is None
            assert manager.store.db.execute("SELECT COUNT(*) FROM jobs WHERE session_id=?", (session_id,)).fetchone()[0] == 0
            with pytest.raises(ProcessLookupError):
                os.kill(process_id, 0)
