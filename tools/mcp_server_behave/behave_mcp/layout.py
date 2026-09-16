"""Where the server keeps its state, so every front-end finds the same file.

Shared by ``behave_mcp`` and ``behave_campaign``; nothing here imports
either.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ENV_VAR = "UBUNTU_PRO_CLIENT_REPO"
STATE_DIR_ENV_VAR = "MCP_STATE_DIR"
DEFAULT_STATE_DIR_NAME = ".mcp_server_behave"
JOBS_SUBDIR = "jobs"
CAMPAIGNS_SUBDIR = "campaigns"
CAMPAIGN_SUFFIX = ".jsonl"


def state_dir(repo_root: Path) -> Path:
    """``$MCP_STATE_DIR`` if set, else ``<repo_root>/.mcp_server_behave``."""
    env_path = os.environ.get(STATE_DIR_ENV_VAR)
    if env_path:
        return Path(env_path).expanduser().resolve()
    return repo_root / DEFAULT_STATE_DIR_NAME


def campaign_dir(repo_root: Path) -> Path:
    return state_dir(repo_root) / CAMPAIGNS_SUBDIR


def campaign_file(repo_root: Path, campaign_id: str) -> Path:
    return campaign_dir(repo_root) / (campaign_id + CAMPAIGN_SUFFIX)
