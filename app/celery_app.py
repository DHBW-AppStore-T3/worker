import logging
import pathlib

from celery import Celery, bootsteps
from celery.signals import task_unknown
from kombu.transport import TRANSPORT_ALIASES

from .config import settings

logger = logging.getLogger(__name__)

# The broker is our own Postgres (.github#5): ``pgq+postgresql://…`` resolves
# to the transport in app/pgq.py. Registered before the app connects.
TRANSPORT_ALIASES.setdefault("pgq", "app.pgq:Transport")

celery_app = Celery("worker", broker=settings.celery_broker_url, include=["app.tasks"])

celery_app.conf.update(
    broker_url=settings.celery_broker_url,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": settings.WORKER_QUEUE_VISIBILITY_SECONDS},
    task_serializer="json",
    accept_content=["json"],
    # A message is deleted only once its job is over; a dead worker's
    # message becomes visible again (lease) and its prologue fails the task
    # as worker_lost instead of running it twice (job_runtime).
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # No result backend: the job writes its outcome into the task row.
    task_ignore_result=True,
    # Celery events and remote control need a fanout exchange, which the
    # pgq transport does not have. Live events go through task_events,
    # cancel through tasks.cancel_requested_at + NOTIFY.
    worker_send_task_events=False,
    task_send_sent_event=False,
    worker_enable_remote_control=False,
    # Upper bound of a job; the soft limit raises inside the job, the hard
    # one kills the process (its lease then runs out: worker_lost).
    task_soft_time_limit=settings.WORKER_JOB_TIMEOUT_SECONDS,
    task_time_limit=settings.WORKER_JOB_TIMEOUT_SECONDS + 300,
)


class HeartbeatFile(bootsteps.StartStopStep):
    """Touch ``WORKER_HEARTBEAT_FILE`` every 30 s; the container healthcheck reads its age.

    Replaces ``celery inspect ping``, which needs remote control.
    """

    requires = {"celery.worker.components:Timer"}
    interval = 30.0

    def __init__(self, worker, **kwargs):
        super().__init__(worker, **kwargs)
        self.tref = None

    def start(self, worker):
        self.touch()
        self.tref = worker.timer.call_repeatedly(self.interval, self.touch, priority=10)

    def stop(self, worker):
        if self.tref is not None:
            self.tref.cancel()
            self.tref = None

    @staticmethod
    def touch():
        try:
            pathlib.Path(settings.WORKER_HEARTBEAT_FILE).touch()
        except OSError as exc:
            logger.warning("cannot touch heartbeat file %s: %s", settings.WORKER_HEARTBEAT_FILE, exc)


celery_app.steps["worker"].add(HeartbeatFile)


@task_unknown.connect
def _fail_unknown_task(sender=None, name=None, id=None, message=None, exc=None, **kwargs):  # noqa: A002
    """A message for a task this worker does not know (API and worker out of step).

    Celery drops it; fail the task row right away instead of leaving it to
    the API's reconciler.
    """
    from . import job_runtime

    job_runtime.fail_unknown(id, name)


if __name__ == "__main__":
    celery_app.start()
