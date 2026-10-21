"""The shared wiring's own behaviour."""

import logging

from behave_mcp.wiring import reconcile_quietly


class FailingService:
    def reconcile(self):
        raise RuntimeError("the state directory is gone")


def test_a_failed_reconcile_is_logged_not_raised(caplog):
    with caplog.at_level(logging.ERROR):
        reconcile_quietly(FailingService())

    assert "reconcile failed" in caplog.text
