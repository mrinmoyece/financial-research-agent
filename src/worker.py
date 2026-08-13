"""Durable research worker and callback-oriented queue runner."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from src.api.contracts import JobStatus, ResearchRequest
from src.api.governance import GovernanceStore
from src.api.job_queue import JobEnvelope, JobQueue
from src.api.job_store import JobStore
from src.config.settings import Settings, get_settings
from src.graph.workflow import run_research
from src.security.identity import Principal

logger = logging.getLogger(__name__)

Processor = Callable[[JobEnvelope], Awaitable[None]]
Maintenance = Callable[[], Awaitable[None]]


async def process_envelope(envelope: JobEnvelope) -> None:
    """Integration hook for applications that prefer subclassing/default wiring."""
    raise NotImplementedError(f"No processor configured for job {envelope.job_id}")


class Worker:
    """Claims one envelope at a time and acknowledges only completed callbacks."""

    def __init__(
        self,
        queue: JobQueue,
        processor: Processor = process_envelope,
        *,
        poll_timeout: float = 1.0,
        maintenance: Maintenance | None = None,
    ) -> None:
        if poll_timeout <= 0:
            raise ValueError("poll_timeout must be positive and bounded")
        self._queue = queue
        self._processor = processor
        self._poll_timeout = poll_timeout
        self._maintenance = maintenance
        self._stopping = asyncio.Event()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def stop(self) -> None:
        """Request graceful shutdown; an active callback is allowed to finish."""
        self._stopping.set()

    async def run_once(self) -> bool:
        """Process at most one envelope. Callback and queue errors propagate."""
        if self._maintenance is not None:
            await self._maintenance()
        envelope = await asyncio.to_thread(self._queue.claim, self._poll_timeout)
        if envelope is None:
            return False
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._renew_lease(envelope, heartbeat_stop))
        try:
            await self._processor(envelope)
        except Exception as exc:
            heartbeat_stop.set()
            await heartbeat
            await asyncio.to_thread(self._queue.retry, envelope, str(exc))
            raise
        heartbeat_stop.set()
        await heartbeat
        acknowledged = await asyncio.to_thread(self._queue.ack, envelope)
        if not acknowledged:
            raise RuntimeError(f"Lease for job {envelope.job_id} was lost before ack")
        return True

    async def _renew_lease(
        self,
        envelope: JobEnvelope,
        stopping: asyncio.Event,
    ) -> None:
        interval = max(min(self._queue.lease_seconds / 3, 30), 0.01)
        while True:
            try:
                await asyncio.wait_for(stopping.wait(), timeout=interval)
                return
            except TimeoutError:
                renewed = await asyncio.to_thread(self._queue.renew, envelope)
                if not renewed:
                    logger.error("Lease for job %s was lost during processing", envelope.job_id)
                    return

    async def run(self) -> None:
        """Run until stopped; stop latency is bounded by ``poll_timeout``."""
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("Job processing failed; retry policy was applied")


class ResearchProcessor:
    """Execute research envelopes and persist governed lifecycle state."""

    def __init__(
        self,
        settings: Settings,
        jobs: JobStore,
        results: JobStore,
        governance: GovernanceStore,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._results = results
        self._governance = governance

    async def __call__(self, envelope: JobEnvelope) -> None:
        raw = self._jobs.get(envelope.job_id)
        if raw is None:
            raise RuntimeError(f"Job {envelope.job_id} is missing from the status store")
        job = JobStatus.model_validate(raw)
        actor = Principal(
            principal_id=envelope.principal_id,
            tenant_id=envelope.tenant_id,
            roles=frozenset({"reader"}),
        )
        job.status = "running"
        job.attempt = envelope.attempt
        self._jobs.set(job.job_id, job.model_dump(mode="json"))
        self._governance.append_audit(
            "job_claimed",
            actor,
            job_id=job.job_id,
            details={"attempt": envelope.attempt},
        )

        try:
            request = ResearchRequest.model_validate(envelope.request)
            state = await run_research(
                query=request.query,
                tickers=request.tickers,
                research_depth=request.research_depth,
            )
            if state.get("error"):
                raise RuntimeError(str(state["error"]))
        except Exception:
            terminal = envelope.attempt >= self._settings.job_max_attempts
            job.status = "failed" if terminal else "retrying"
            job.error = (
                "Research execution failed after all retry attempts."
                if terminal
                else "Research execution failed and will be retried."
            )
            if terminal:
                job.completed_at = datetime.now(UTC).isoformat()
                self._governance.release_job_slot(envelope.tenant_id)
            self._jobs.set(job.job_id, job.model_dump(mode="json"))
            self._governance.append_audit(
                "job_failed" if terminal else "job_retry_scheduled",
                actor,
                job_id=job.job_id,
                details={"attempt": envelope.attempt},
            )
            raise

        report = state.get("report")
        stored_state = dict(state)
        stored_state["report"] = _report_dict(report)
        self._results.set(job.job_id, stored_state)
        job.status = "awaiting_approval" if self._settings.require_report_approval else "completed"
        job.report = None if self._settings.require_report_approval else _report_dict(report)
        job.error = None
        job.completed_at = datetime.now(UTC).isoformat()
        job.tool_calls_count = len(state.get("tool_calls_log", []))
        job.llm_calls_count = int(state.get("model_calls", 0))
        job.input_tokens = int(state.get("input_tokens", 0))
        job.output_tokens = int(state.get("output_tokens", 0))
        self._jobs.set(job.job_id, job.model_dump(mode="json"))
        self._governance.release_job_slot(envelope.tenant_id)
        self._governance.append_audit(
            "report_awaiting_approval"
            if self._settings.require_report_approval
            else "job_completed",
            actor,
            job_id=job.job_id,
            details={
                "tool_calls": job.tool_calls_count,
                "model_calls": job.llm_calls_count,
            },
        )

    async def reconcile_dead_letters(self, queue: JobQueue) -> None:
        """Finalize jobs dead-lettered by lease recovery after worker termination."""
        envelopes = await asyncio.to_thread(queue.dead_letters)
        for envelope in envelopes:
            raw = self._jobs.get(envelope.job_id)
            if raw is None:
                logger.error("Dead-lettered job %s has no status record", envelope.job_id)
                continue
            job = JobStatus.model_validate(raw)
            if job.status in {
                "failed",
                "completed",
                "awaiting_approval",
                "approved",
                "rejected",
            }:
                await asyncio.to_thread(queue.remove_dead_letter, envelope)
                continue
            job.status = "failed"
            job.error = "Research execution failed after all retry attempts."
            job.completed_at = datetime.now(UTC).isoformat()
            job.attempt = envelope.attempt
            self._jobs.set(job.job_id, job.model_dump(mode="json"))
            self._governance.release_job_slot(envelope.tenant_id)
            actor = Principal(
                principal_id=envelope.principal_id,
                tenant_id=envelope.tenant_id,
                roles=frozenset({"reader"}),
            )
            self._governance.append_audit(
                "job_dead_lettered",
                actor,
                job_id=job.job_id,
                details={"attempt": envelope.attempt, "reason": envelope.last_error},
            )
            await asyncio.to_thread(queue.remove_dead_letter, envelope)


def _report_dict(report: object) -> dict[str, object] | None:
    if report is None:
        return None
    if isinstance(report, dict):
        return {str(key): value for key, value in report.items()}
    model_dump = getattr(report, "model_dump", None)
    if callable(model_dump):
        dumped: Any = model_dump(mode="json")
        if isinstance(dumped, dict):
            return {str(key): value for key, value in dumped.items()}
    raise TypeError("Research report is not serializable")


async def run_worker() -> None:
    """Construct production dependencies and run until SIGTERM/SIGINT."""
    settings = get_settings()
    queue = JobQueue(settings)
    jobs = JobStore(settings, namespace="status")
    results = JobStore(settings, namespace="result")
    governance = GovernanceStore(settings)
    processor = ResearchProcessor(settings, jobs, results, governance)
    worker = Worker(
        queue,
        processor,
        poll_timeout=settings.job_poll_timeout_seconds,
        maintenance=lambda: processor.reconcile_dead_letters(queue),
    )
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, worker.stop)
    await worker.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
