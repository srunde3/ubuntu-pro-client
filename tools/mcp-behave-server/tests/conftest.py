"""Shared fixtures/helpers for the test files in this directory.
"""

import json
from pathlib import Path

import pytest

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


class FakeWorkspace:
    """A ``Workspace``-shaped test double usable across test files."""

    def __init__(
        self,
        *,
        repo_root=None,
        log_dir=None,
        env=None,
        repo_root_error=None,
    ):
        self._repo_root = repo_root
        self._log_dir = log_dir
        self._env = env if env is not None else {}
        self._repo_root_error = repo_root_error

    def resolve_repo_root(self, override):
        if self._repo_root_error is not None:
            raise ValueError(self._repo_root_error)
        if override:
            return Path(override)
        return self._repo_root

    def resolve_log_dir(self, repo_root):
        return self._log_dir

    def subprocess_env(self):
        return dict(self._env)


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
