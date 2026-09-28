"""Bounded checkpoints for a streaming assistant event, never executable calls."""

import asyncio
import sqlite3


DRAFT_INTERVAL_SECONDS = 1.0
DRAFT_BYTES = 16384
STORAGE_ERRORS = (OSError, sqlite3.Error)


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
        self.worker = asyncio.create_task(self._periodic())

    def changed(self, size=1):
        if self.failure:
            raise self.failure
        self.dirty_bytes += max(size, 1)
        if self.dirty_bytes >= DRAFT_BYTES:
            self.flush()

    def flush(self):
        if self.dirty_bytes:
            try:
                self.store.save(self.session)
            except STORAGE_ERRORS as exc:
                raise DraftPersistenceError(exc) from exc
            self.dirty_bytes = 0

    async def _periodic(self):
        while True:
            await asyncio.sleep(DRAFT_INTERVAL_SECONDS)
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
