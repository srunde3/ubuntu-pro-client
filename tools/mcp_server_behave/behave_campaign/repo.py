"""Read the checkout state a campaign was built from."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .domain import RepoState

_TIMEOUT_SECONDS = 10


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def repo_state(repo_root: Path) -> RepoState:
    """Return commit, branch, and dirty state, or nulls outside a checkout."""
    commit = _git(repo_root, "rev-parse", "HEAD")
    if commit is None:
        return RepoState(root=str(repo_root))

    status = _git(repo_root, "status", "--porcelain")
    return RepoState(
        root=str(repo_root),
        commit=commit,
        branch=_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD"),
        dirty=None if status is None else bool(status),
    )
