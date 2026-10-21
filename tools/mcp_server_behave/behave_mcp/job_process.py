"""The process each behave job runs under.

``python -m behave_mcp.job_process <job dir> [--lane-fd N] -- <command>``

The launcher hands this process the job's lane lock and its job lock, and
then lets go of both, so they are held as long as this process runs the
job. It runs the command (tox) as a child in its own process group, writes
the child's output to the job's ``stdout.log``, and when the child exits
writes the job's ``exit.json`` once, by rename. Then it lets go of the lane
and reconciles, so the next queued job takes that lane at once, and exits.

Its own session keeps the server's death or signals from reaching it. The
child gets a parent-death signal, so it cannot outlive this process. Taken
together: a job lock that is free with no ``exit.json`` means exactly
that the job died without finishing.

SIGTERM is how a job is killed: it is passed to the child's process group,
and this process keeps waiting, so pycloudlib can clean up what it made.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Sequence

from behave_mcp.adapters import LocalJobResultStore
from behave_mcp.domain import layout
from behave_mcp.messages import JobExit
from behave_mcp.wiring import create_service, reconcile_quietly

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
    def __init__(self, job_dir: Path) -> None:
        self._paths = layout.job_paths(job_dir.parent, job_dir.name)
        self._store = LocalJobResultStore(job_dir.parent)
        self._job_id = job_dir.name
        self._child: subprocess.Popen | None = None
        self.killed = False

    def kill(self, signum: int, frame: FrameType | None) -> None:
        """Pass a kill on to the child's group, and keep waiting for it."""
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
                    self._record(None, error="killed before it started")
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
            self._record(None, error=str(error))
            return
        self._record(returncode)

    def _record(self, returncode: int | None, error: str = "") -> None:
        self._store.write_exit(
            self._job_id,
            JobExit(
                finished_at=_now(),
                returncode=returncode,
                ok=returncode == 0 and not self.killed,
                killed=self.killed,
                error=error,
            ),
        )


USAGE = (
    "usage: python -m behave_mcp.job_process JOB_DIR [--lane-fd N] -- "
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
    job = JobProcess(job_dir)
    signal.signal(signal.SIGTERM, job.kill)
    signal.signal(signal.SIGINT, job.kill)
    job.ready()
    job.run(args[2:])
    if lane_fd is not None:
        os.close(lane_fd)
    # jobs/<id>/ lives in the state directory's jobs/.
    reconcile_quietly(create_service(state_dir=job_dir.parent.parent))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
