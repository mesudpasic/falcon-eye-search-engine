import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class ScanError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class Job:
    kind: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    status: str = "queued"
    progress: float = 0.0
    message: str = "Waiting"
    error: str | None = None
    folder: dict | None = None
    scan: dict | None = None
    gallery: dict | None = None
    created: float = field(default_factory=time.time)
    task: object | None = None

    def touch(self, fraction: float, message: str) -> None:
        self.progress = max(0.0, min(0.99, float(fraction)))
        self.message = message


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str) -> Job:
        job = Job(kind=kind)
        with self._lock:
            self._jobs[str(job.id)] = job
            if len(self._jobs) > 40:
                finished = [
                    item
                    for item in self._jobs.values()
                    if item.status in {"done", "error"}
                ]
                finished.sort(key=lambda item: item.created)
                for old in finished[:-12]:
                    self._jobs.pop(str(old.id), None)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)


async def finish(job: Job, work) -> None:
    job.status = "running"
    if job.message == "Waiting":
        job.message = "Starting"
    try:
        await work(job)
    except ScanError as exc:
        job.status = "error"
        job.error = exc.message
        job.message = exc.message
        logger.info("job %s stopped: %s", job.id, exc.message)
        return
    except Exception:
        logger.exception("job %s failed", job.id)
        job.status = "error"
        job.error = "The scan failed."
        job.message = "The scan failed."
        return
    if job.status == "running":
        job.status = "done"
        job.progress = 1.0
