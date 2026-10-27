"""The worker's database access: psycopg 3, plain SQL, role ``appstore_worker``.

The worker claims tasks, writes task events, results and leases; it never
needs the ORM models of the API. Every connection is in autocommit mode
unless a block asks for a transaction.
"""

from __future__ import annotations

import os
import socket
import uuid

import psycopg

from .config import settings

# Tells this process apart from an earlier one with the same host name and
# pid (a container restarted in its pod is pid 1 again).
_INSTANCE = uuid.uuid4().hex[:8]


def libpq_url(url: str) -> str:
    """``url`` without a SQLAlchemy driver suffix, e.g. ``postgresql+psycopg2://`` -> ``postgresql://``."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    return f"{scheme.split('+', 1)[0]}://{rest}"


def connect(purpose: str) -> psycopg.Connection:
    """A new autocommit connection, labelled ``worker-<purpose>:<pid>`` in ``pg_stat_activity``."""
    return psycopg.connect(
        libpq_url(settings.DATABASE_URL),
        autocommit=True,
        application_name=f"worker-{purpose}:{os.getpid()}",
    )


def worker_id() -> str:
    """Who holds a task: host, process id and instance of the worker process running it."""
    return f"{socket.gethostname()}:{os.getpid()}:{_INSTANCE}"
