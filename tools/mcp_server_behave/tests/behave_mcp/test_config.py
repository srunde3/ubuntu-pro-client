import pytest

from behave_mcp.config import (
    REMOVED_ENV_VARS,
    ConfigError,
    Settings,
    ignored_behave_settings,
    load_settings,
)


def test_load_settings_defaults():
    settings = load_settings({})
    assert settings == Settings(
        campaign_poll_timeout=60,
        host="127.0.0.1",
        port=8000,
    )


def test_behave_settings_in_the_environment_are_reported_as_ignored():
    environ = {"PATH": "/bin", "UACLIENT_BEHAVE_CONTRACT_TOKEN": "x"}

    assert ignored_behave_settings(environ) == [
        "UACLIENT_BEHAVE_CONTRACT_TOKEN"
    ]


# For each variable the server no longer reads, words its error must name.
REMOVED_GUIDANCE = {
    "MCP_MAX_PARALLEL_JOBS": "max_jobs.lxd",
    "MCP_MAX_LXD_JOBS": "max_jobs.lxd",
    "MCP_MAX_CLOUD_JOBS": "max_jobs.cloud",
    "MCP_MAX_WSL_JOBS": "max_jobs.wsl",
    "MCP_ALLOW_CLOUD_MACHINE_TYPES": "max_jobs.cloud",
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
