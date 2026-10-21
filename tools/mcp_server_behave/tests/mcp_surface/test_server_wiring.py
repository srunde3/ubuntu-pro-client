"""How the server assembles its long-lived campaign collaborators.

The runner and the per-call service must share one campaign store: a reader
blocked in await_campaign_events is woken by the very object the runner
appends through.
"""

import pytest

import behave_mcp.server as server_module
from tests.conftest import make_repo_with_feature

pytestmark = pytest.mark.integration


@pytest.fixture
def fresh_server(tmp_path, monkeypatch):
    """A server with no campaign collaborators built yet."""
    repo_root = make_repo_with_feature(tmp_path)
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server_module, "_runner", None)
    monkeypatch.setattr(server_module, "_store", None)
    return server_module


def test_the_service_and_runner_share_one_store(fresh_server):
    service = fresh_server.campaign_service()
    runner = fresh_server.campaign_runner()

    assert service._store is runner._store


def test_they_share_it_even_when_the_runner_is_built_first(fresh_server):
    # control_campaign can be the first campaign tool called, for instance
    # on a campaign left over from a previous run.
    runner = fresh_server.campaign_runner()
    service = fresh_server.campaign_service()

    assert service._store is runner._store


def test_the_runner_is_built_once(fresh_server):
    assert fresh_server.campaign_runner() is fresh_server.campaign_runner()


def test_the_store_is_built_once(fresh_server):
    service = fresh_server.campaign_service()
    again = fresh_server.campaign_service()

    assert service._store is again._store
