"""The capacity pool each machine type draws on.

The machine types are copied from ``features/machine_types.py``; add new
ones here by hand.
"""

from collections.abc import Iterable
from enum import Enum


class Pool(str, Enum):
    """A separately limited kind of job capacity."""

    LXD = "lxd"
    CLOUD = "cloud"
    WSL = "wsl"

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, spec: str) -> str:
        return format(str(self.value), spec)


POOLS = tuple(pool.value for pool in Pool)

POOL_OF: dict[str, Pool] = {
    "aws.generic": Pool.CLOUD,
    "aws.pro": Pool.CLOUD,
    "aws.pro-fips": Pool.CLOUD,
    "azure.generic": Pool.CLOUD,
    "azure.pro": Pool.CLOUD,
    "azure.pro-fips": Pool.CLOUD,
    "gcp.generic": Pool.CLOUD,
    "gcp.pro": Pool.CLOUD,
    "gcp.pro-fips": Pool.CLOUD,
    "lxd-container": Pool.LXD,
    "lxd-vm": Pool.LXD,
    "wsl": Pool.WSL,
}

# WSL jobs share one Windows host and cannot run side by side.
MAX_WSL_JOBS = 1


def pools_of(machine_types: Iterable[str]) -> frozenset[Pool]:
    """The pools a job on these machine types draws on."""
    return frozenset(POOL_OF[machine_type] for machine_type in machine_types)


POOL_LIMIT_ENV_VARS: dict[Pool, str] = {
    Pool.LXD: "MCP_MAX_LXD_JOBS",
    Pool.CLOUD: "MCP_MAX_CLOUD_JOBS",
    Pool.WSL: "MCP_MAX_WSL_JOBS",
}


# Cloud jobs cost money, so they stay off until a limit is set.
DEFAULT_POOL_LIMITS: dict[Pool, int] = {
    Pool.LXD: 1,
    Pool.CLOUD: 0,
    Pool.WSL: 1,
}


POOL_LIMIT_CAPS: dict[Pool, int] = {Pool.WSL: MAX_WSL_JOBS}
