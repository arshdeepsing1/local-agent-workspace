"""A chat's live messages as newline-delimited JSON, for scripts (client.py, scripts/agent_run.py).

GET /api/sessions/{id}/stream.ndjson sends what the page's WebSocket gets, one JSON object per line: first the
snapshot, then every broadcast (events, deltas, status, context, changes, ...). A turn ends with the idle status
that carries its result record (turn_result, turns.py). A keep_alive line every STREAM_KEEPALIVE_SECONDS lets a
reader tell a quiet chat from a dropped connection. The local token is checked like every /api route.

The stream is one more listener of the chat (AgentManager.listeners), so a broadcast never waits for it: a send
only queues the message, and a reader that falls STREAM_QUEUE_LIMIT messages behind is dropped, as a stalled page
is (its stream ends with a stream_closed line; reconnecting gives a fresh snapshot).

A stream never ends on its own, and uvicorn waits for open responses before the app's shutdown (which stops the
running turns), so the server (serve.Server) ends the streams first: close_streams, reason 'shutdown'.
"""
import asyncio
import json
import weakref

from fastapi.responses import StreamingResponse

from .agents import public_session

STREAM_QUEUE_LIMIT = 1_000
STREAM_KEEPALIVE_SECONDS = 15.0
SNAPSHOT_EVENTS = 200  # The first snapshot's latest events, extended to the whole latest turn (at most 1,000).


def stream_snapshot(session, status, compaction=None):
    """The first snapshot a page or a script gets: the latest 200 events, extended to the whole latest turn (at most
    1,000); earlier ones come from GET /api/sessions/{id}/events. compaction: AgentManager.compaction_view."""
    snapshot = public_session(session, status, compaction)
    events, total = snapshot["events"], len(snapshot["events"])
    start = max(0, total - SNAPSHOT_EVENTS)
    latest_user = next((index for index in range(total - 1, -1, -1) if events[index].get("type") == "user"), None)
    if latest_user is not None and latest_user < start:
        start = max(latest_user, total - 1000)
    snapshot.update(events=events[start:], events_start=start, events_total=total)
    return snapshot


OPEN_STREAMS = weakref.WeakSet()  # Every StreamListener not yet ended (close_streams).


class StreamListener:
    """An NDJSON reader as a chat listener: send_json queues a message without waiting; close ends the stream."""

    def __init__(self, limit=STREAM_QUEUE_LIMIT):
        self.queue = asyncio.Queue(maxsize=limit)
        self.closed = None  # Why the stream ends: "slow_reader", "closed" or "shutdown".
        try:
            self.loop = asyncio.get_running_loop()  # The server whose shutdown ends it.
        except RuntimeError:
            self.loop = None
        OPEN_STREAMS.add(self)

    async def send_json(self, data):
        if self.closed:
            raise ConnectionError("This stream has ended.")
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            self.closed = "slow_reader"
            raise ConnectionError("The stream's reader fell behind.") from None

    def end(self, reason="closed"):
        self.closed = self.closed or reason
        OPEN_STREAMS.discard(self)
        try:
            self.queue.put_nowait(None)  # Wakes the reader; a full queue ends once it is read.
        except asyncio.QueueFull:
            pass

    async def close(self, code=1000):
        self.end()


def close_streams(loop=None):
    """End every open stream of the event loop (default: the running one), as the server begins to shut down:
    each reader gets what was queued and a stream_closed line with reason 'shutdown'."""
    loop = loop or asyncio.get_running_loop()
    for listener in list(OPEN_STREAMS):
        if listener.loop is loop:
            listener.end("shutdown")


def line(data):
    return (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def register_stream(app, manager, session_or_404):
    @app.get("/api/sessions/{session_id}/stream.ndjson")
    async def stream_ndjson(session_id: str):
        session = session_or_404(session_id)
        await manager.refresh_meter_limits(session)  # Settings may have changed since it was last open.
        listener = StreamListener(STREAM_QUEUE_LIMIT)
        listener.queue.put_nowait({"type": "snapshot", "session": stream_snapshot(session, manager.statuses.get(session_id, "idle"), manager.compaction_view(session_id))})
        manager.listeners.setdefault(session_id, set()).add(listener)
        try:
            tasks = manager.task_board.list(session_id)
        except ValueError:  # Deleted while connecting.
            tasks = []
        if tasks:
            await listener.send_json({"type": "tasks", "tasks": tasks})
        await manager.publish_changes(session)

        async def lines():
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(listener.queue.get(), STREAM_KEEPALIVE_SECONDS)
                    except TimeoutError:
                        if listener.closed:
                            break
                        yield line({"type": "keep_alive"})
                        continue
                    if item is None:
                        break
                    yield line(item)
                    if listener.closed and listener.queue.empty():
                        break
                yield line({"type": "stream_closed", "reason": listener.closed or "closed"})
            finally:
                manager.listeners.get(session_id, set()).discard(listener)
                OPEN_STREAMS.discard(listener)

        return StreamingResponse(lines(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
