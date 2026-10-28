"""Shared fixtures/helpers for the test files in this directory.
"""

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

import pro_behave.mcp.server as server_module
from pro_behave.behave.framework import BehaveFramework
from pro_behave.campaign.ports import QueueRefused
from pro_behave.jobs.adapters import (
    LocalJobResultStore,
    LocalJobResultStoreFactory,
)
from pro_behave.jobs.domain.config import CheckoutConfig
from pro_behave.jobs.domain.pools import DEFAULT_MAX_JOBS
from pro_behave.jobs.messages import JobExit, RepoState
from pro_behave.jobs.service import JobService


@pytest.fixture(autouse=True)
def private_state_dir(monkeypatch, tmp_path):
    """Every front-end reconciles; none may touch a real checkout's state.
    A test that needs its own state directory sets this again."""
    monkeypatch.setenv("PRO_BEHAVE_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def fresh_campaign_store(monkeypatch):
    """The server builds its campaign store on first use; each test gets
    its own, bound to that test's state directory."""
    monkeypatch.setattr(server_module, "_store", None)


# One scenario covering every pool, so a test can start a job on any of
# them. Its name and combos are what run_job must be given.
UNIT_SCENARIO = "Attach command"


UNIT_FEATURE = """Feature: sample

  Scenario Outline: Attach command
    Given a `<release>` `<machine_type>` machine

    Examples: ubuntu release
      | release | machine_type  |
      | jammy   | lxd-container |
      | noble   | lxd-vm        |
      | jammy   | wsl           |
      | jammy   | azure.generic |
      | jammy   | aws.pro-fips  |
"""


def job_file(jobs: Path, job_id: str, name: str) -> Path:
    """The path of one of a job's files, with its directory created."""
    job_dir = jobs / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir / name


def make_repo_with_feature(
    tmp_path: Path,
    rel: str | None = "features/cli/sample.feature",
    *,
    name: str = "repo",
) -> Path:
    """Build a minimal valid repo_root: a tox.ini plus one feature file.

    Pass ``rel=None`` to get just the ``features/`` directory with no
    feature file, for tests that only care about repo_root validation.
    """
    repo_root = tmp_path / name
    if rel is None:
        (repo_root / "features").mkdir(parents=True)
    else:
        feature_path = repo_root / rel
        feature_path.parent.mkdir(parents=True, exist_ok=True)
        feature_path.write_text(UNIT_FEATURE, encoding="utf-8")
    (repo_root / "tox.ini").write_text("[tox]\n", encoding="utf-8")
    return repo_root


class FakeLease:
    """A lane taken from a ``FakeLanePool``.

    The launcher closes its own hold once a job runs, but the lane stays
    busy until the job ends, as it does when the job process holds the lock.
    """

    def __init__(self, pool, lanes):
        self.pool = pool
        self.closed = False
        self._lanes = lanes
        self._released = False

    def close(self):
        self.closed = True

    def release(self):
        if not self._released:
            self._released = True
            self._lanes.held[self.pool] -= 1


class FakeLanePool:
    """A ``LanePool`` counting held lanes per pool."""

    def __init__(self):
        self.held = {}

    def take(self, pool, max_jobs):
        if self.held.get(pool, 0) >= max_jobs:
            return None
        self.held[pool] = self.held.get(pool, 0) + 1
        return FakeLease(pool, self)

    def busy(self, pool):
        return self.held.get(pool, 0)


class FakeJobLauncher:
    """A ``JobLauncher`` whose jobs run until the test ends them.

    ``finish`` writes the exit the way the job process does; ``crash`` ends a
    job without one, as a job process killed outright would. With
    ``finish_on_launch`` (``finish`` keywords), each job ends at once.
    """

    def __init__(
        self,
        jobs_dir=None,
        *,
        error=None,
        unkillable=(),
        finish_on_launch=None,
    ):
        self.jobs_dir = jobs_dir
        self.calls = []
        self.running = set()
        self.killed = []
        self._leases = {}
        self._error = error
        self._unkillable = set(unkillable)
        self._finish_on_launch = finish_on_launch

    def launch(self, job_id, command, cwd, env, lease, record):
        self.calls.append(
            {"job_id": job_id, "command": command, "cwd": cwd, "env": env}
        )
        # The real launcher holds the job's lock by now.
        record()
        lease.close()
        if self._error is not None:
            lease.release()
            raise self._error
        self.running.add(job_id)
        self._leases[job_id] = lease
        if self._finish_on_launch is not None:
            self.finish(job_id, **self._finish_on_launch)

    def is_running(self, job_id):
        return job_id in self.running

    def kill(self, job_id):
        self.killed.append(job_id)
        return job_id in self.running and job_id not in self._unkillable

    def finish(self, job_id, returncode=0, killed=False, log="", report=None):
        """End a job: its log, its report, then its exit, in that order."""
        store = LocalJobResultStore(self.jobs_dir)
        paths = store.write_targets(job_id)
        if log:
            paths.stdout_log.write_text(log, encoding="utf-8")
        if report is not None:
            paths.json_report.write_text(json.dumps(report), encoding="utf-8")
        store.write_exit(
            job_id,
            JobExit(
                finished_at="T1",
                returncode=returncode,
                ok=returncode == 0 and not killed,
                killed=killed,
            ),
        )
        self._end(job_id)

    def crash(self, job_id):
        """End a job with no exit recorded."""
        self._end(job_id)

    def _end(self, job_id):
        self.running.discard(job_id)
        lease = self._leases.pop(job_id, None)
        if lease is not None:
            lease.release()


class FakeConfigStore:
    """A ``ConfigStore`` in memory; ``error`` makes every read fail."""

    def __init__(self, max_jobs=None, behave=None, *, error=None):
        self.config = CheckoutConfig(
            max_jobs={**DEFAULT_MAX_JOBS, **(max_jobs or {})},
            behave=dict(behave or {}),
        )
        self._error = error

    def read(self):
        if self._error is not None:
            raise ValueError(self._error)
        return self.config

    def write(self, config):
        self.config = config


class FakeReconcileLock:
    """A ``ReconcileLock`` that counts how often it was held."""

    def __init__(self):
        self.held = 0

    @contextmanager
    def hold(self):
        self.held += 1
        yield


def make_service(workspace, **ports) -> JobService:
    """A ``JobService`` over fakes; any port or callable can be given."""
    defaults = dict(
        config=FakeConfigStore(),
        framework=BehaveFramework(),
        results=LocalJobResultStoreFactory(),
        lanes=FakeLanePool(),
        launcher=FakeJobLauncher(),
        reconcile_lock=FakeReconcileLock(),
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
        now_utc=lambda: "T0",
        new_job_id=lambda: "job0001",
    )
    defaults.update(ports)
    return JobService(workspace=workspace, **defaults)


class FakeWorkspace:
    """A ``Workspace``-shaped test double usable across test files."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        jobs_dir: Path | None = None,
        env: dict[str, str] | None = None,
        repo_root_error: str | None = None,
        repo_state: RepoState | None = None,
        campaign_dir: Path | None = None,
    ) -> None:
        self._repo_root = repo_root
        self._jobs_dir = jobs_dir
        self._campaign_dir = campaign_dir
        self._env = env if env is not None else {}
        self._repo_root_error = repo_root_error
        self._repo_state = (
            repo_state if repo_state is not None else RepoState()
        )

    def resolve_repo_root(self, override: str | None) -> Path:
        if self._repo_root_error is not None:
            raise ValueError(self._repo_root_error)
        if override:
            return Path(override)
        if self._repo_root is None:
            raise ValueError(
                "FakeWorkspace has no repo_root configured and no "
                "override was given"
            )
        return self._repo_root

    def resolve_jobs_dir(self) -> Path:
        if self._jobs_dir is None:
            raise ValueError("FakeWorkspace has no jobs_dir configured")
        return self._jobs_dir

    def resolve_campaign_dir(self) -> Path:
        if self._campaign_dir is None:
            raise ValueError("FakeWorkspace has no campaign_dir configured")
        return self._campaign_dir

    def subprocess_env(self) -> dict[str, str]:
        return dict(self._env)

    def repo_state(self, repo_root: Path) -> RepoState:
        return self._repo_state.model_copy(update={"root": str(repo_root)})


def result_json(result):
    assert result.isError is False
    for block in result.content:
        if hasattr(block, "text"):
            return json.loads(block.text)
    raise AssertionError("Expected text content block in tool result")


def result_error_text(result):
    """Text content of a failed tool call - MCP's own isError signal."""
    assert result.isError is True
    for block in result.content:
        if hasattr(block, "text"):
            return block.text
    raise AssertionError("Expected text content block in tool error result")


def job_result(passed=1, failed=0, job_id="job", failures=()):
    """A finished job as ``JobRunner.poll`` reports it."""
    return {
        "state": "finished",
        "ok": failed == 0,
        "job_id": job_id,
        "summary": {
            "scenarios": {"passed": passed, "failed": failed, "skipped": 0},
            "features": {},
        },
        "failures": list(failures),
    }


class FakeJobRunner:
    """A ``JobRunner`` over fake jobs.

    A queued job runs until the test calls ``finish`` or ``kill``. With
    ``auto_finish`` (``job_result`` keywords, ``{}`` for a pass), each job
    finishes on its first poll and reports it on the next, the way a real
    job is seen running at least once. A unit in ``full_pools`` is refused.
    ``started_at`` is when every running job began.
    """

    def __init__(
        self,
        *,
        full_pools=(),
        auto_finish=None,
        unkillable=(),
        started_at="",
    ):
        self.queued = []
        self.results = {}
        self.killed = []
        self.unkillable = set(unkillable)
        self.full_pools = set(full_pools)
        self.auto_finish = auto_finish
        self.started_at = started_at
        self.refusals = 0
        self.locks = 0
        self.job_reconciles = 0

    @contextmanager
    def lock(self):
        self.locks += 1
        yield

    def new_job_id(self):
        return "job{}".format(len(self.queued) + self.refusals + 1)

    def queue(
        self, job_id, unit, *, repo_root, install_from, campaign_id, record
    ):
        if unit.pool in self.full_pools:
            self.refusals += 1
            raise QueueRefused("pool is full", unit.pool)
        record()
        self.queued.append((unit, job_id, install_from, repo_root))

    def poll(self, job_id):
        if job_id in self.results:
            return self.results[job_id]
        if job_id not in {queued[1] for queued in self.queued}:
            return {"state": "interrupted", "reason": "died", "job_id": job_id}
        if self.auto_finish is not None:
            self.finish(job_id, **self.auto_finish)
        return {
            "state": "running",
            "job_id": job_id,
            "started_at": self.started_at,
        }

    def finish(self, job_id, **kwargs):
        self.results[job_id] = job_result(job_id=job_id, **kwargs)

    def kill(self, job_id):
        """Kill a job; it reports itself interrupted on its next poll.

        Returns False for a job that already ended, or one in
        ``unkillable``, as the real runner does for a job whose process
        cannot be reached.
        """
        self.killed.append(job_id)
        if job_id in self.results or job_id in self.unkillable:
            return False
        self.results[job_id] = {
            "state": "interrupted",
            "reason": "killed",
            "ok": False,
            "job_id": job_id,
        }
        return True

    def reconcile_jobs(self):
        self.job_reconciles += 1

    @property
    def queued_units(self):
        return [unit for unit, _, _, _ in self.queued]


class FakeFeatureReader:
    """A ``FeatureReader`` over a fixed unit list, filtered as discovery is."""

    def __init__(self, units):
        self.units = list(units)
        self.seen_scope = None

    def discover_units(self, repo_root, scope):
        self.seen_scope = scope
        return [unit for unit in self.units if scope.matches_unit(unit)]


class FakeJobFiles:
    """``JobFiles`` that are all present, except those named in ``without``."""

    def __init__(self, tmp_path, without=()):
        self.tmp_path = tmp_path
        self.without = set(without)

    def files(self, job_id):
        found = {}
        for name in ("job.json", "exit.json", "report.json", "stdout.log"):
            if (job_id, name) in self.without:
                continue
            path = self.tmp_path / "jobs" / job_id / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
            found[name] = path
        return found


class FakeBundleWriter:
    """A ``BundleWriter`` that keeps the entries it would have written."""

    def __init__(self):
        self.output = None
        self.top = None
        self.entries = {}

    def write(self, output, top, entries):
        self.output, self.top = output, top
        self.entries = {entry.name: entry for entry in entries}
        return []
