"""The ingestion worker: a separate process that prepares uploaded datasets and documents.

Run locally:  cd backend && uv run python -m app.worker

It long-polls the queue, processes each message (see app/ingest/jobs.py), and every
`SWEEP_EVERY` seconds runs the safety nets: re-send jobs whose message was lost, mark
dead-lettered jobs failed, and remove uploaded files still stored for deleted sources.
Ctrl+C / SIGTERM finishes the current job, then exits, so a normal shutdown never abandons
work halfway (and an abnormal one is covered by the job lock expiring).
"""

import signal
import time

import structlog

from app.answer import cache
from app.db import _session_factory
from app.ingest import jobs, lifecycle

log = structlog.get_logger()

SWEEP_EVERY = 30  # seconds


class Worker:
    def __init__(self) -> None:
        self.stopping = False
        self._last_sweep = 0.0

    def stop(self, *_: object) -> None:
        log.info("worker_stopping")
        self.stopping = True

    def run_once(self, wait_seconds: int = 10) -> int:
        """Receive and process one batch. Returns the number of messages handled."""
        messages = jobs.receive(wait_seconds=wait_seconds)
        for message in messages:
            with _session_factory()() as db:
                jobs.handle_message(db, message)
        if time.monotonic() - self._last_sweep >= SWEEP_EVERY:
            self.safety_nets()
        return len(messages)

    def safety_nets(self) -> None:
        self._last_sweep = time.monotonic()
        with _session_factory()() as db:
            jobs.sweep(db)
            jobs.drain_dead_letters(db)
            lifecycle.purge_deleted_uploads(db)
            cache.purge_expired(db)

    def run_forever(self) -> None:
        jobs.ensure_infrastructure()
        log.info("worker_started")
        while not self.stopping:
            try:
                self.run_once()
            except Exception as e:  # e.g. the queue is briefly unreachable: wait, don't die
                log.warning("worker_loop_error", error=f"{type(e).__name__}: {e}")
                time.sleep(5)
        log.info("worker_stopped")


def main() -> None:
    worker = Worker()
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
