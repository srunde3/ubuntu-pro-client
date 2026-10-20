import pytest

from behave_mcp.config import (
    REMOVED_ENV_VARS,
    ConfigError,
    Settings,
    load_settings,
)
from behave_mcp.domain.pools import Pool


def test_load_settings_defaults():
    settings = load_settings({})
    assert settings == Settings(
        pool_limits={Pool.LXD: 1, Pool.CLOUD: 0, Pool.WSL: 1},
        campaign_poll_timeout=60,
        host="127.0.0.1",
        port=8000,
    )


def test_load_settings_pool_limits():
    settings = load_settings(
        {
            "MCP_MAX_LXD_JOBS": "8",
            "MCP_MAX_CLOUD_JOBS": "6",
            "MCP_MAX_WSL_JOBS": "0",
        }
    )
    assert settings.pool_limits == {
        Pool.LXD: 8,
        Pool.CLOUD: 6,
        Pool.WSL: 0,
    }


@pytest.mark.parametrize(
    "name", ["MCP_MAX_LXD_JOBS", "MCP_MAX_CLOUD_JOBS", "MCP_MAX_WSL_JOBS"]
)
@pytest.mark.parametrize("raw", ["abc", "-1"])
def test_load_settings_pool_limit_invalid_raises(name, raw):
    with pytest.raises(ConfigError, match=name):
        load_settings({name: raw})


def test_load_settings_wsl_limit_above_one_raises():
    with pytest.raises(ConfigError, match="between 0 and 1"):
        load_settings({"MCP_MAX_WSL_JOBS": "2"})


# For each variable the server no longer reads, words its error must name.
REMOVED_GUIDANCE = {
    "MCP_MAX_PARALLEL_JOBS": "MCP_MAX_LXD_JOBS",
    "MCP_ALLOW_CLOUD_MACHINE_TYPES": "MCP_MAX_CLOUD_JOBS",
    "MCP_TRANSPORT": "streamable HTTP",
}


def test_every_removed_variable_has_guidance():
    assert set(REMOVED_GUIDANCE) == set(REMOVED_ENV_VARS)


@pytest.mark.parametrize("name, guidance", REMOVED_GUIDANCE.items())
def test_a_removed_variable_stops_startup_with_guidance(name, guidance):
    with pytest.raises(
        ConfigError, match=f"{name} is no longer read.*{guidance}"
    ):
        load_settings({name: "1"})


def test_load_settings_host_override():
    assert load_settings({"MCP_HOST": "0.0.0.0"}).host == "0.0.0.0"


def test_load_settings_port_override():
    assert load_settings({"MCP_PORT": "9001"}).port == 9001


def test_load_settings_port_non_integer_raises():
    with pytest.raises(ConfigError, match="MCP_PORT"):
        load_settings({"MCP_PORT": "abc"})


def test_load_settings_port_out_of_range_raises():
    with pytest.raises(ConfigError, match="MCP_PORT"):
        load_settings({"MCP_PORT": "70000"})
    with pytest.raises(ConfigError, match="MCP_PORT"):
        load_settings({"MCP_PORT": "0"})


def test_campaign_poll_timeout_is_configurable():
    settings = load_settings({"MCP_CAMPAIGN_POLL_TIMEOUT": "90"})

    assert settings.campaign_poll_timeout == 90


@pytest.mark.parametrize("value", ["0", "-1", "121", "soon", "1.5"])
def test_an_unusable_campaign_poll_timeout_is_rejected(value):
    # The ceiling exists because the client's own request timeout is the
    # real limit; a value above it would hang rather than return.
    with pytest.raises(ConfigError) as error:
        load_settings({"MCP_CAMPAIGN_POLL_TIMEOUT": value})

    assert "MCP_CAMPAIGN_POLL_TIMEOUT" in str(error.value)
