"""Shared fixtures/helpers for the test files in this directory.
"""

import json
from pathlib import Path

import pytest

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
    ) -> None:
        self._repo_root = repo_root
        self._log_dir = log_dir
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
