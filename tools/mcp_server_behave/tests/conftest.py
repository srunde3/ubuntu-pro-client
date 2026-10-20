"""Shared fixtures/helpers for the test files in this directory.
"""

import json
from pathlib import Path

import pytest

from behave_campaign.ports import CampaignLockedError, LaneStartError
from behave_mcp.messages import RepoState
from behave_mcp.server import registry


@pytest.fixture(autouse=True)
def clear_jobs():
    registry.clear()
    yield
    registry.clear()


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
        feature_path.write_text("Feature: sample\n", encoding="utf-8")
    (repo_root / "tox.ini").write_text("[tox]\n", encoding="utf-8")
    return repo_root


class FakeProcessHandle:
    """Fake process handle: doubles as a bare ``subprocess.Popen`` return
    value (``PopenLauncher`` tests) and as a ``ProcessHandle`` port
    implementation (``Job.process_handle`` in service/registry tests)."""

    def __init__(self, returncode=None, pid=4242):
        self.returncode = returncode
        self.pid = pid
        self.closed = False
        self.terminated = False

    def poll(self):
        return self.returncode

    def close(self):
        self.closed = True

    def terminate(self):
        self.terminated = True


class FakeLauncher:
    """A ``ProcessLauncher``-shaped test double usable across test files."""

    def __init__(
        self,
        handle: FakeProcessHandle | None = None,
        error: Exception | None = None,
        alive_pids: set[int] | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._handle = handle if handle is not None else FakeProcessHandle()
        self._error = error
        self._alive_pids = set(alive_pids) if alive_pids else set()

    def launch(self, command, cwd, env, stdout_log_path):
        self.calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env": env,
                "stdout_log_path": stdout_log_path,
            }
        )
        if self._error is not None:
            raise self._error
        return self._handle

    def is_pid_alive(self, pid: int) -> bool:
        return pid in self._alive_pids


class FakeWorkspace:
    """A ``Workspace``-shaped test double usable across test files."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        log_dir: Path | None = None,
        env: dict[str, str] | None = None,
        repo_root_error: str | None = None,
        repo_state: RepoState | None = None,
        campaign_dir: Path | None = None,
    ) -> None:
        self._repo_root = repo_root
        self._log_dir = log_dir
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

    def resolve_log_dir(self, repo_root: Path) -> Path:
        if self._log_dir is None:
            raise ValueError("FakeWorkspace has no log_dir configured")
        return self._log_dir

    def resolve_campaign_dir(self, repo_root: Path) -> Path:
        if self._campaign_dir is None:
            raise ValueError("FakeWorkspace has no campaign_dir configured")
        return self._campaign_dir

    def subprocess_env(self) -> dict[str, str]:
        return dict(self._env)

    def repo_state(self, repo_root: Path) -> RepoState:
        return self._repo_state


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


class FakeProcess:
    """A fake Popen-like handle usable across the MCP protocol test files."""

    def __init__(self, report_path=None, pid=5555):
        self._report_path = report_path
        self._poll_count = 0
        self.returncode = None
        self.pid = pid

    def poll(self):
        self._poll_count += 1
        if (
            self._report_path is not None
            and self._poll_count == 2
            and self.returncode is None
        ):
            self._report_path.write_text(
                json.dumps(
                    [
                        {
                            "name": "attach feature",
                            "elements": [
                                {
                                    "name": "attach scenario",
                                    "steps": [
                                        {
                                            "name": "do attach",
                                            "result": {"status": "passed"},
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            self.returncode = 0
        return self.returncode


def job_result(passed=1, failed=0, job_id="job", failures=()):
    """A completed job as ``LaneRunner.poll`` reports it."""
    return {
        "status": "completed",
        "ok": failed == 0,
        "job_id": job_id,
        "summary": {
            "scenarios": {"passed": passed, "failed": failed, "skipped": 0},
            "features": {},
        },
        "failures": list(failures),
    }


class FakeLanes:
    """A ``LaneRunner`` that starts fake jobs.

    A job finishes when the test calls ``finish``. With ``auto_finish``
    (``job_result`` keywords, ``{}`` for a pass), each job finishes on its
    first poll instead and reports it on the next, the way a real job is
    seen running at least once.
    """

    def __init__(self, *, max_starts=None, full_pools=(), auto_finish=None):
        self.started = []
        self.results = {}
        self.max_starts = max_starts
        self.full_pools = set(full_pools)
        self.auto_finish = auto_finish
        self.refusals = 0

    def start(self, unit, *, repo_root, install_from):
        if unit.pool in self.full_pools:
            self.refusals += 1
            raise LaneStartError("pool is full", full_pool=unit.pool)
        if (
            self.max_starts is not None
            and len(self.started) >= self.max_starts
        ):
            self.refusals += 1
            raise LaneStartError("at capacity")
        job_id = "job{}".format(len(self.started) + 1)
        self.started.append((unit, job_id, install_from, repo_root))
        return job_id

    def poll(self, job_id):
        if job_id in self.results or self.auto_finish is None:
            return self.results.get(job_id)
        self.finish(job_id, **self.auto_finish)
        return None

    def finish(self, job_id, **kwargs):
        self.results[job_id] = job_result(job_id=job_id, **kwargs)

    @property
    def started_units(self):
        return [unit for unit, _, _, _ in self.started]


class FakeTicker:
    """A ``Ticker`` that captures the tick callable and never calls it.

    Tests call ``runner.tick`` themselves, or ``drive`` to tick until the
    runner reports the campaign finished, as the real thread would.
    """

    # A campaign of a few units finishes in a handful of ticks; this only
    # stops a bug from looping forever.
    MAX_TICKS = 200

    def __init__(self):
        self.tick = None
        self.stopped = 0

    def start(self, tick):
        self.tick = tick

    def stop(self):
        self.stopped += 1
        self.tick = None

    def is_running(self):
        return self.tick is not None

    def drive(self):
        assert self.tick is not None, "the ticker was never started"
        for _ in range(self.MAX_TICKS):
            if self.tick is None or self.tick():
                return
        raise AssertionError(
            "campaign still unfinished after {} ticks".format(self.MAX_TICKS)
        )


class FakeLock:
    """A ``CampaignRunLock`` that records what it was asked."""

    def __init__(self, taken_by_other=False):
        self.held = set()
        self.acquired = []
        self.released = []
        self._taken_by_other = taken_by_other

    def acquire(self, campaign_id):
        if self._taken_by_other:
            raise CampaignLockedError("held elsewhere")
        self.held.add(campaign_id)
        self.acquired.append(campaign_id)

    def release(self, campaign_id):
        self.held.discard(campaign_id)
        self.released.append(campaign_id)

    def held_by_other(self, campaign_id):
        return self._taken_by_other


class FakeFeatureReader:
    """A ``FeatureReader`` over a fixed unit list, filtered as discovery is."""

    def __init__(self, units):
        self.units = list(units)
        self.seen_filters = None

    def discover_units(self, repo_root, filters):
        self.seen_filters = filters
        return [unit for unit in self.units if filters.matches_unit(unit)]

    def available_dimensions(self, repo_root):
        def counted(field):
            scenarios = {}
            for unit in self.units:
                scenarios.setdefault(getattr(unit, field), set()).add(
                    (unit.feature, unit.scenario)
                )
            return [
                {"name": name, "scenario_count": len(found)}
                for name, found in sorted(scenarios.items())
            ]

        return {
            "releases": counted("release"),
            "machine_types": counted("machine_type"),
        }


class FakeJobFiles:
    """``JobFiles`` that are all present, except those named in ``without``."""

    def __init__(self, tmp_path, without=()):
        self.tmp_path = tmp_path
        self.without = set(without)

    def files(self, job_id):
        found = {}
        for name in ("meta.json", "report.json", "stdout.log"):
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
