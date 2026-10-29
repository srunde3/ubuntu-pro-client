"""The process each job runs under.

``python -m pro_e2e.job_process <job dir> [--lane-fd N] -- <command>``

The launcher hands this process the job's lane lock and its job lock, and
then lets go of both, so they are held as long as this process runs the
job. It runs the command (tox) as a child in its own process group, writes
the child's output to the job's ``stdout.log``, and when the child exits
decides the unit's result from the job's files and writes ``result.json``
once, by rename. Then it lets go of the lane and reconciles, so the next
queued job takes that lane at once, and exits.

Its own session keeps the server's death or signals from reaching it. The
child gets a parent-death signal, so it cannot outlive this process. Taken
together: a job lock that is free with no ``result.json`` means exactly
that the job died without finishing.

SIGTERM is how a job is killed: it is passed to the child's process group,
and this process keeps waiting, so pycloudlib can clean up what it made. A
SIGTERM after the child exits is ignored, so it cannot stop the result
being written.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Sequence

from pro_e2e.behave.framework import BehaveFramework
from pro_e2e.jobs.adapters import (
    FileConfigStore,
    FileLanePool,
    FileReconcileLock,
    JobProcessLauncher,
    LocalJobResultStore,
    LocalJobResultStoreFactory,
    LocalWorkspace,
)
from pro_e2e.jobs.domain import layout
from pro_e2e.jobs.domain.results import (
    finished_result,
    killed_result,
    start_failure,
    unreadable_result,
)
from pro_e2e.jobs.messages import JobResult
from pro_e2e.jobs.ports import Framework
from pro_e2e.jobs.service import JobService

# The test framework every job runs under.
FRAMEWORK = BehaveFramework()

# The module a job process runs as, for the launcher.
ENTRY = "pro_e2e.job_process"

# From <sys/prctl.h>: deliver a signal to this process when its parent dies.
PR_SET_PDEATHSIG = 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _die_with(parent: int) -> None:
    """In the child, before exec: take SIGTERM when ``parent`` dies.

    The parent may already be gone by the time the request is made, in
    which case it would never fire, so that is checked afterwards.
    """
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    if os.getppid() != parent:
        os._exit(1)


class JobProcess:
    def __init__(self, job_dir: Path, framework: Framework) -> None:
        self._paths = layout.job_paths(job_dir.parent, job_dir.name)
        self._store = LocalJobResultStore(job_dir.parent)
        self._job_id = job_dir.name
        self._framework = framework
        self._child: subprocess.Popen | None = None
        self.killed = False
        # Set once the command has exited: a kill after that changes nothing.
        self._ended = False

    def kill(self, signum: int, frame: FrameType | None) -> None:
        """Pass a kill on to the child's group, and keep waiting for it."""
        if self._ended:
            return
        self.killed = True
        if self._child is not None:
            try:
                os.killpg(self._child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def ready(self) -> None:
        """Publish this process's PID, now that it can take a kill.

        A kill that came sooner left its marker instead of a signal.
        """
        self._paths.lock.write_text(str(os.getpid()))
        if self._paths.kill.exists():
            self.killed = True

    def run(self, command: Sequence[str]) -> None:
        parent = os.getpid()
        try:
            with self._paths.stdout_log.open("w") as log:
                if self.killed:
                    self._write(
                        killed_result(
                            _now(), reason="killed before it started"
                        )
                    )
                    return
                self._child = subprocess.Popen(
                    list(command),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=lambda: _die_with(parent),
                )
                if self.killed:
                    self.kill(signal.SIGTERM, None)
                returncode = self._child.wait()
        except OSError as error:
            self._ended = True
            self._write(start_failure(_now(), str(error)))
            return
        self._ended = True
        self._write(self._result(returncode))

    def _result(self, returncode: int) -> JobResult:
        """The job's result, decided from its files. Nothing here can stop
        the result being written: a failure becomes an ``error`` result."""
        if self.killed:
            return killed_result(_now(), returncode)
        try:
            request = self._store.read_request(self._job_id)
            observed = self._framework.observe(
                self._paths.json_report, self._paths.stdout_log
            )
            return finished_result(
                _now(),
                returncode,
                request.instances if request is not None else [],
                observed,
            )
        except Exception as error:
            return unreadable_result(_now(), returncode, error)

    def _write(self, result: JobResult) -> None:
        self._store.write_result(self._job_id, result)


USAGE = (
    "usage: python -m pro_e2e.job_process JOB_DIR [--lane-fd N] -- "
    "COMMAND..."
)


def main(argv: Sequence[str]) -> int:
    args = list(argv)
    lane_fd: int | None = None
    if len(args) > 2 and args[1] == "--lane-fd":
        lane_fd = int(args[2])
        del args[1:3]
    if len(args) < 3 or args[1] != "--":
        print(USAGE)
        return 2
    job_dir = Path(args[0])
    job = JobProcess(job_dir, FRAMEWORK)
    signal.signal(signal.SIGTERM, job.kill)
    signal.signal(signal.SIGINT, job.kill)
    job.ready()
    job.run(args[2:])
    if lane_fd is not None:
        os.close(lane_fd)
    # jobs/<id>/ lives in the state directory's jobs/.
    _reconcile(job_dir.parent.parent)
    return 0


def _job_service(state_dir: Path) -> JobService:
    workspace = LocalWorkspace(state_dir)
    return JobService(
        workspace=workspace,
        config=FileConfigStore(workspace.resolve_state_dir),
        framework=FRAMEWORK,
        results=LocalJobResultStoreFactory(),
        lanes=FileLanePool(
            lambda: layout.lanes_dir(workspace.resolve_state_dir())
        ),
        launcher=JobProcessLauncher(workspace.resolve_jobs_dir, ENTRY),
        reconcile_lock=FileReconcileLock(workspace.resolve_state_dir),
        monotonic=time.monotonic,
        sleep=time.sleep,
        now_utc=_now,
        new_job_id=lambda: uuid.uuid4().hex[:8],
    )


def _reconcile(state_dir: Path) -> None:
    """Start the next queued jobs. The job is over either way: nobody is
    waiting on this process to report a failure to."""
    try:
        _job_service(state_dir).reconcile()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
