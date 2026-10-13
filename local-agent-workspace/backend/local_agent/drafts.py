"""Bounded checkpoints for a streaming assistant event, never executable calls."""

import asyncio
import sqlite3
import time


DRAFT_INTERVAL_SECONDS = 1.0
DRAFT_BYTES = 16384
# Checkpoints take at most about a twentieth of streaming time: a save of a large conversation
# rewrites its whole file on the event loop, which every chat and the page share.
DRAFT_COST_FACTOR = 20
STORAGE_ERRORS = (OSError, sqlite3.Error)


def _now():
    return time.monotonic()


class DraftPersistenceError(OSError):
    def __init__(self, cause):
        super().__init__("Local history could not be saved. The response was stopped; recent text may be missing "
                         f"after restart. Check available disk space and folder permissions. ({cause})")


class StreamDraft:
    def __init__(self, store, session):
        self.store, self.session = store, session
        self.dirty_bytes = 0
        self.failure = None
        self.owner = asyncio.current_task()
        self.cancelled_owner = False
        self.last_cost = 0.0  # Seconds the last checkpoint's save took.
        self.last_flush = None  # When it ended (_now()).
        self.worker = asyncio.create_task(self._periodic())

    def spacing(self):
        """Seconds a checkpoint waits after the last one, by how long that one's save took."""
        return DRAFT_COST_FACTOR * self.last_cost

    def changed(self, size=1):
        if self.failure:
            raise self.failure
        self.dirty_bytes += max(size, 1)
        if self.dirty_bytes >= DRAFT_BYTES and (self.last_flush is None
                                                 or _now() - self.last_flush >= self.spacing()):
            self.flush()

    def flush(self):
        if self.dirty_bytes:
            started = _now()
            try:
                self.store.save(self.session)
            except STORAGE_ERRORS as exc:
                raise DraftPersistenceError(exc) from exc
            self.dirty_bytes = 0
            self.last_flush = _now()
            self.last_cost = self.last_flush - started

    def next_wait(self):
        """Seconds until the next periodic checkpoint: DRAFT_INTERVAL_SECONDS, or longer while the
        last save's spacing has not passed."""
        if self.last_flush is None:
            return DRAFT_INTERVAL_SECONDS
        return max(DRAFT_INTERVAL_SECONDS, self.last_flush + self.spacing() - _now())

    async def _periodic(self):
        while True:
            await asyncio.sleep(self.next_wait())
            try:
                self.flush()
            except DraftPersistenceError as exc:
                self.failure = exc
                # Wake a stalled stream so persistence errors cannot disappear in
                # a detached task while the provider keeps the request open.
                if not self.owner.cancelling():
                    self.cancelled_owner = True
                    self.owner.cancel()
                return

    async def close(self):
        self.worker.cancel()
        results = await asyncio.gather(self.worker, return_exceptions=True)
        if self.cancelled_owner:
            self.owner.uncancel()
        if isinstance(results[0], BaseException) and not isinstance(results[0], asyncio.CancelledError):
            raise results[0]
