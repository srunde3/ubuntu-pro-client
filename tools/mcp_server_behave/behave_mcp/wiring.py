"""The production wiring of the job service, shared by every front-end.

The MCP server, the CLI and each job process build the service here, so
they all read and write one state directory the same way.
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from behave_mcp.adapters import (
    FileConfigStore,
    FileLanePool,
    FileReconcileLock,
    JobProcessLauncher,
    LocalFeatureFileReader,
    LocalJobResultStoreFactory,
    LocalWorkspace,
)
from behave_mcp.domain import layout
from behave_mcp.service import BehaveService, BehaveServiceError

__all__ = [
    "BehaveService",
    "BehaveServiceError",
    "create_service",
    "reconcile_quietly",
    "utc_timestamp",
]

logger = logging.getLogger(__name__)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def create_service(state_dir: Path | None = None) -> BehaveService:
    """Wire the production adapters into a ``BehaveService``.

    Everything lives in the state directory, resolved per call unless
    ``state_dir`` fixes it: nothing about a job is held in this process.
    """
    workspace = LocalWorkspace(state_dir)
    return BehaveService(
        workspace=workspace,
        config=FileConfigStore(workspace.resolve_state_dir),
        feature_reader=LocalFeatureFileReader(),
        results=LocalJobResultStoreFactory(),
        lanes=FileLanePool(
            lambda: layout.lanes_dir(workspace.resolve_state_dir())
        ),
        launcher=JobProcessLauncher(workspace.resolve_jobs_dir),
        reconcile_lock=FileReconcileLock(workspace.resolve_state_dir),
        monotonic=time.monotonic,
        sleep=time.sleep,
        now_utc=utc_timestamp,
        new_job_id=lambda: uuid.uuid4().hex[:8],
    )


def reconcile_quietly(service: BehaveService) -> None:
    """Reconcile, logging rather than raising what goes wrong.

    For callers with a result of their own to return: a failed reconcile
    is retried by the next caller, and must not turn a good answer into an
    error.
    """
    try:
        service.reconcile()
    except Exception:
        logger.exception("reconcile failed; the next caller retries it")
