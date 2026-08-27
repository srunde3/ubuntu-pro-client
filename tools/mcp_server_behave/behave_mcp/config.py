"""Startup configuration parsing for the behave MCP server.

Settings are parsed and validated once, when the server starts. Invalid
values raise ``ConfigError`` loudly rather than degrading at request time.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from behave_mcp import domain

TRANSPORT_ENV_VAR = "MCP_TRANSPORT"
HOST_ENV_VAR = "MCP_HOST"
PORT_ENV_VAR = "MCP_PORT"
_ALLOWED_TRANSPORTS: tuple[str, ...] = ("stdio", "sse", "streamable-http")
_DEFAULT_TRANSPORT = "stdio"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_TRUTHY_FLAG_VALUES = {"1", "true", "yes", "on"}

Transport = Literal["stdio", "sse", "streamable-http"]


class ConfigError(Exception):
    """Raised when environment configuration is invalid at startup."""


@dataclass(frozen=True)
class Settings:
    """Validated server settings parsed once at startup."""

    allow_cloud_machine_types: bool
    max_parallel_jobs: int
    transport: Transport
    host: str
    port: int


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Parse and validate settings from ``environ``.

    Raises ConfigError if any value is present but unusable.
    """
    return Settings(
        allow_cloud_machine_types=_parse_flag(
            environ, domain.ALLOW_CLOUD_MACHINE_TYPES_ENV_VAR
        ),
        max_parallel_jobs=_parse_max_parallel_jobs(environ),
        transport=_parse_transport(environ),
        host=environ.get(HOST_ENV_VAR, "").strip() or _DEFAULT_HOST,
        port=_parse_port(environ),
    )


def _parse_flag(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() in _TRUTHY_FLAG_VALUES


def _parse_max_parallel_jobs(environ: Mapping[str, str]) -> int:
    raw = environ.get(domain.MAX_PARALLEL_JOBS_ENV_VAR, "").strip()
    if not raw:
        return domain.DEFAULT_MAX_PARALLEL_JOBS

    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"{domain.MAX_PARALLEL_JOBS_ENV_VAR} must be a positive "
            f"integer, got {raw!r}"
        )

    if value <= 0:
        raise ConfigError(
            f"{domain.MAX_PARALLEL_JOBS_ENV_VAR} must be a positive "
            f"integer, got {raw!r}"
        )

    return value


def _parse_transport(environ: Mapping[str, str]) -> Transport:
    raw = environ.get(TRANSPORT_ENV_VAR, "").strip() or _DEFAULT_TRANSPORT
    if raw not in _ALLOWED_TRANSPORTS:
        raise ConfigError(
            f"{TRANSPORT_ENV_VAR} must be one of "
            f"{', '.join(_ALLOWED_TRANSPORTS)}, got {raw!r}"
        )
    return cast(Transport, raw)


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
