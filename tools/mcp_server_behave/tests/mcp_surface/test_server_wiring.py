"""How the server assembles its campaign collaborators.

Every campaign service the server builds shares one campaign store: a
reader blocked in await_campaign_events is woken by the very object a
reconcile in this process appends through.
"""

import pytest

import behave_mcp.server as server_module
from tests.conftest import make_repo_with_feature

pytestmark = pytest.mark.integration


@pytest.fixture
def fresh_server(tmp_path, monkeypatch):
    """A server with no campaign store built yet."""
    repo_root = make_repo_with_feature(tmp_path)
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server_module, "_store", None)
    return server_module


def test_every_campaign_service_shares_one_store(fresh_server):
    service = fresh_server.campaign_service()
    again = fresh_server.campaign_service()

    assert service._store is again._store


def test_a_campaign_service_can_queue_jobs(fresh_server):
    # control_campaign and every reconcile need the job service.
    service = fresh_server.campaign_service()

    assert service._jobs is not None
