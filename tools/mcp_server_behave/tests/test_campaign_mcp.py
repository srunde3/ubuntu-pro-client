"""Campaign tools over the real MCP protocol, in process."""

import pytest
from conftest import result_error_text, result_json
from mcp.shared.memory import create_connected_server_and_client_session

from behave_mcp.server import mcp

FEATURE = """Feature: Example feature

  Scenario Outline: Runs everywhere
    Given a `<release>` `<machine_type>` machine
    Then I verify that `esm-infra` is enabled

    Examples: ubuntu release
      | release | machine_type  |
      | jammy   | lxd-container |
      | noble   | lxd-vm        |
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A minimal checkout, with campaign files kept out of it."""
    repo_root = tmp_path / "repo"
    features = repo_root / "features"
    features.mkdir(parents=True)
    (features / "example.feature").write_text(FEATURE)
    (repo_root / "tox.ini").write_text("[tox]\n")
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_CAMPAIGN_DIR", str(tmp_path / "campaigns"))
    return repo_root


@pytest.mark.asyncio
async def test_create_campaign_plans_units_without_starting_them(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "create_campaign", {"campaign_id": "1234567"}
        )

    campaign = result_json(result)["campaign"]

    assert campaign["campaign_id"] == "1234567"
    assert campaign["total_units"] == 2
    assert campaign["counts"]["unattempted"] == 2
    assert campaign["counts"]["running"] == 0


@pytest.mark.asyncio
async def test_create_campaign_records_scope_and_install_source(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "create_campaign",
            {
                "campaign_id": "1234567",
                "releases": ["jammy"],
                "install_from": "proposed",
            },
        )

    campaign = result_json(result)["campaign"]

    assert campaign["total_units"] == 1
    assert campaign["scope"]["release"] == ["jammy"]
    assert campaign["install_from"] == "proposed"


@pytest.mark.asyncio
async def test_creating_the_same_campaign_twice_is_an_error(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "create_campaign", {"campaign_id": "1234567"}
        )

    assert "already holds a campaign" in result_error_text(result)


@pytest.mark.asyncio
async def test_more_lanes_than_the_server_allows_is_an_error(repo):
    # MCP_MAX_PARALLEL_JOBS defaults to 1, so 8 lanes cannot be honoured.
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "create_campaign", {"campaign_id": "1234567", "max_lanes": 8}
        )

    assert "concurrent job limit" in result_error_text(result)


@pytest.mark.asyncio
async def test_an_unknown_release_is_an_error(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "create_campaign",
            {"campaign_id": "1234567", "releases": ["bogus"]},
        )

    assert "unknown release" in result_error_text(result)


@pytest.mark.asyncio
async def test_list_campaigns_reports_created_campaigns(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "111"})
        await client.call_tool(
            "create_campaign",
            {"campaign_id": "222", "releases": ["jammy"]},
        )
        result = await client.call_tool("list_campaigns", {})

    campaigns = result_json(result)["campaigns"]

    assert [c["campaign_id"] for c in campaigns] == ["111", "222"]
    assert [c["total_units"] for c in campaigns] == [2, 1]


@pytest.mark.asyncio
async def test_list_campaigns_is_empty_before_any_exist(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("list_campaigns", {})

    assert result_json(result)["campaigns"] == []


@pytest.mark.asyncio
async def test_campaign_status_omits_units_by_default(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "campaign_status", {"campaign_id": "1234567"}
        )

    payload = result_json(result)

    assert payload["units"] is None
    assert payload["running"] == []
    assert payload["problems"] == []
    assert payload["campaign"]["counts"]["unattempted"] == 2


@pytest.mark.asyncio
async def test_campaign_status_returns_units_when_a_limit_is_given(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "campaign_status",
            {"campaign_id": "1234567", "units_limit": 50},
        )

    units = result_json(result)["units"]

    assert len(units) == 2
    assert {unit["release"] for unit in units} == {"jammy", "noble"}
    assert all(unit["state"] == "unattempted" for unit in units)


@pytest.mark.asyncio
async def test_campaign_status_filters_by_release(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "campaign_status",
            {
                "campaign_id": "1234567",
                "release": "noble",
                "units_limit": 50,
            },
        )

    payload = result_json(result)

    assert payload["campaign"]["counts"]["unattempted"] == 1
    assert [unit["release"] for unit in payload["units"]] == ["noble"]


@pytest.mark.asyncio
async def test_campaign_status_on_an_unknown_campaign_is_an_error(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "campaign_status", {"campaign_id": "absent"}
        )

    assert "no campaign" in result_error_text(result)
