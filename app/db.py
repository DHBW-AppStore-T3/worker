"""The worker's database access: psycopg 3, plain SQL, role ``appstore_worker``.

The worker writes task events, results and leases; it never needs the ORM
models of the API. Connections are per process (prefork children open
their own after the fork) and in autocommit mode unless a block asks for
a transaction.
"""

from __future__ import annotations

import os
import socket

import psycopg

from .config import settings
from .pgq import libpq_url


def connect(purpose: str) -> psycopg.Connection:
    """A new autocommit connection, labelled ``worker-<purpose>:<pid>`` in ``pg_stat_activity``."""
    return psycopg.connect(
        libpq_url(settings.DATABASE_URL),
        autocommit=True,
        application_name=f"worker-{purpose}:{os.getpid()}",
    )


def worker_id() -> str:
    """Who holds a task: host and process id of the process running it."""
    return f"{socket.gethostname()}:{os.getpid()}"
