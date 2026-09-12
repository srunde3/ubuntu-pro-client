import pytest

from behave_mcp.config import ConfigError, Settings, load_settings


def test_load_settings_defaults():
    settings = load_settings({})
    assert settings == Settings(
        allow_cloud_machine_types=False,
        max_parallel_jobs=1,
        transport="stdio",
        host="127.0.0.1",
        port=8000,
    )


def test_load_settings_allow_cloud_toggle():
    assert (
        load_settings(
            {"MCP_ALLOW_CLOUD_MACHINE_TYPES": "yes"}
        ).allow_cloud_machine_types
        is True
    )
    assert (
        load_settings(
            {"MCP_ALLOW_CLOUD_MACHINE_TYPES": "no"}
        ).allow_cloud_machine_types
        is False
    )


def test_load_settings_max_parallel_valid():
    assert load_settings({"MCP_MAX_PARALLEL_JOBS": "4"}).max_parallel_jobs == 4


def test_load_settings_max_parallel_non_integer_raises():
    with pytest.raises(ConfigError, match="positive integer"):
        load_settings({"MCP_MAX_PARALLEL_JOBS": "abc"})


def test_load_settings_max_parallel_non_positive_raises():
    with pytest.raises(ConfigError, match="positive integer"):
        load_settings({"MCP_MAX_PARALLEL_JOBS": "0"})


def test_load_settings_transport_override():
    assert load_settings({"MCP_TRANSPORT": "sse"}).transport == "sse"


def test_load_settings_transport_invalid_raises():
    with pytest.raises(ConfigError, match="MCP_TRANSPORT"):
        load_settings({"MCP_TRANSPORT": "carrier-pigeon"})


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
