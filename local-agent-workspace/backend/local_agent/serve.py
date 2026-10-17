"""Serving the app with uvicorn on this computer (run.py and scripts/simulate_databricks.py).

uvicorn waits for every open HTTP response before it runs the app's own shutdown, which stops the running turns,
ends the commands and writes the clean-shutdown marker. A chat's stream.ndjson (stream_api.py) never ends on its
own while a script reads it, so Server ends the streams first; a page's WebSocket is closed by uvicorn itself.
GRACEFUL_SHUTDOWN_SECONDS bounds the wait for any other response that is still open.
"""
import sys

import uvicorn

from .stream_api import close_streams

GRACEFUL_SHUTDOWN_SECONDS = 5


class Server(uvicorn.Server):
    async def shutdown(self, sockets=None):
        close_streams()
        await super().shutdown(sockets)


def config(app, port, **options):
    """uvicorn's Config for app (the app or its factory's import string) on 127.0.0.1:port."""
    return uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, factory=isinstance(app, str),
                          timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS, **options)


def run(app, port, reload=False):
    """uvicorn.run with Server. --reload (development) uses uvicorn's own server in the reloaded process: there an
    open stream holds a shutdown for up to GRACEFUL_SHUTDOWN_SECONDS."""
    if reload:
        uvicorn.run(app, host="127.0.0.1", port=port, factory=True, reload=True, access_log=False,
                    timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS)
        return
    server = Server(config(app, port))
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    if not server.started:
        sys.exit(3)  # uvicorn's exit code for a failed start-up.
