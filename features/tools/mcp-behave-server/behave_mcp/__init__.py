"""Behave MCP server package."""

import sys
from pathlib import Path

# `features.behave_features` (parsing/summarizing .feature files) is owned
# by features/ -- it's the repo's authority on .feature file conventions,
# not this package's -- see features/behave_features.py. This package runs
# in its own isolated venv (launched via
# `uvx --from features/tools/mcp-behave-server`), so it can't see the rest
# of the repo checkout without this bridge.
_REPO_ROOT = str(Path(__file__).resolve().parents[4])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
