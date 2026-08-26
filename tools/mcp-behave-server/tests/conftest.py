"""Shared fixtures/helpers for the MCP protocol-level test files.

``clear_jobs`` applies automatically to every test in this directory.
``result_json``, ``result_error_text``, and ``FakeProcess`` look unused
here -- they're imported directly by test_mcp_integration.py and
test_mcp_e2e.py, which is the whole point of keeping them in conftest.py.
"""

import json

import pytest

from behave_mcp.server import registry


@pytest.fixture(autouse=True)
def clear_jobs():
    registry.clear()
    yield
    registry.clear()


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
