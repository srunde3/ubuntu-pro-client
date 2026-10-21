"""The capacity pool each machine type draws on.

The machine types are copied from ``features/machine_types.py``; add new
ones here by hand.
"""

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


# Cloud jobs cost money, so they stay off until max jobs is set.
DEFAULT_MAX_JOBS: dict[Pool, int] = {
    Pool.LXD: 1,
    Pool.CLOUD: 0,
    Pool.WSL: 1,
}


MAX_JOBS_CAPS: dict[Pool, int] = {Pool.WSL: MAX_WSL_JOBS}


def max_jobs_error(pool: Pool, value: object) -> str | None:
    """Why ``value`` cannot be ``pool``'s max jobs, or None if it can."""
    cap = MAX_JOBS_CAPS.get(pool)
    expected = (
        f"an integer between 0 and {cap}"
        if cap is not None
        else "a non-negative integer"
    )
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or (cap is not None and value > cap)
    ):
        return f"{pool} max jobs must be {expected}, got {value!r}"
    return None
