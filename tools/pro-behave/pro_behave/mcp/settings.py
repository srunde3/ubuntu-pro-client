"""The MCP server's own settings, read from its environment.

Settings are parsed and validated once, when the server starts. Invalid
values raise ``ConfigError`` loudly rather than degrading at request time.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from pro_behave.jobs.domain.config import BEHAVE_ENV_PREFIX

HOST_ENV_VAR = "PRO_BEHAVE_MCP_HOST"
PORT_ENV_VAR = "PRO_BEHAVE_MCP_PORT"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000

# What to do instead, for each variable the server no longer reads.
REMOVED_ENV_VARS: dict[str, str] = {
    "MCP_MAX_PARALLEL_JOBS": "use pro-behave config set max_jobs.lxd N",
    "MCP_MAX_LXD_JOBS": "use pro-behave config set max_jobs.lxd N",
    "MCP_MAX_CLOUD_JOBS": "use pro-behave config set max_jobs.cloud N",
    "MCP_MAX_WSL_JOBS": "use pro-behave config set max_jobs.wsl N",
    "MCP_ALLOW_CLOUD_MACHINE_TYPES": (
        "use pro-behave config set max_jobs.cloud N"
    ),
    "MCP_TRANSPORT": (
        "the server always serves streamable HTTP on PRO_BEHAVE_MCP_HOST "
        "and PRO_BEHAVE_MCP_PORT"
    ),
    "MCP_HOST": "use PRO_BEHAVE_MCP_HOST",
    "MCP_PORT": "use PRO_BEHAVE_MCP_PORT",
    "MCP_CAMPAIGN_POLL_TIMEOUT": "use PRO_BEHAVE_MCP_POLL_TIMEOUT",
    "MCP_STATE_DIR": "use PRO_BEHAVE_STATE_DIR",
}


CAMPAIGN_POLL_TIMEOUT_ENV_VAR = "PRO_BEHAVE_MCP_POLL_TIMEOUT"


# How long await_campaign_events may hold a request open. The real ceiling
# is the client's own request timeout, which varies per host and is not ours
# to set, so this is configurable and capped conservatively.
DEFAULT_CAMPAIGN_POLL_TIMEOUT = 60


MAX_CAMPAIGN_POLL_TIMEOUT = 120


class ConfigError(Exception):
    """Raised when environment configuration is invalid at startup."""


@dataclass(frozen=True)
class Settings:
    """Validated server settings parsed once at startup."""

    campaign_poll_timeout: int
    host: str
    port: int


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Parse and validate settings from ``environ``.

    Raises ConfigError if any value is present but unusable.
    """
    _reject_removed(environ)
    return Settings(
        campaign_poll_timeout=_parse_campaign_poll_timeout(environ),
        host=environ.get(HOST_ENV_VAR, "").strip() or _DEFAULT_HOST,
        port=_parse_port(environ),
    )


def _reject_removed(environ: Mapping[str, str]) -> None:
    for name, instead in REMOVED_ENV_VARS.items():
        if environ.get(name, "").strip():
            raise ConfigError(f"{name} is no longer read: {instead}")


def ignored_behave_settings(environ: Mapping[str, str]) -> list[str]:
    """The behave settings in ``environ``, which no job will see.

    A job gets behave settings only from the service's config, so it is the
    same whoever starts it.
    """
    return sorted(
        name for name in environ if name.startswith(BEHAVE_ENV_PREFIX)
    )


def _parse_campaign_poll_timeout(environ: Mapping[str, str]) -> int:
    raw = environ.get(CAMPAIGN_POLL_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_CAMPAIGN_POLL_TIMEOUT

    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"{CAMPAIGN_POLL_TIMEOUT_ENV_VAR} must be an integer "
            f"between 1 and {MAX_CAMPAIGN_POLL_TIMEOUT}, got {raw!r}"
        )

    if not 0 < value <= MAX_CAMPAIGN_POLL_TIMEOUT:
        raise ConfigError(
            f"{CAMPAIGN_POLL_TIMEOUT_ENV_VAR} must be an integer "
            f"between 1 and {MAX_CAMPAIGN_POLL_TIMEOUT}, got {raw!r}"
        )

    return value


def _parse_port(environ: Mapping[str, str]) -> int:
    raw = environ.get(PORT_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_PORT

    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"{PORT_ENV_VAR} must be a valid port number, got {raw!r}"
        )

    if not 0 < value < 65536:
        raise ConfigError(
            f"{PORT_ENV_VAR} must be a valid port number, got {raw!r}"
        )

    return value
