import pytest

from pro_behave.jobs.domain.pools import POOL_OF, Pool, max_jobs_error


class TestPools:
    def test_pro_fips_types_are_cloud(self):
        assert {POOL_OF["aws.pro-fips"], POOL_OF["gcp.pro-fips"]} == {
            Pool.CLOUD
        }

    def test_lxd_types_share_one_pool(self):
        assert {POOL_OF["lxd-vm"], POOL_OF["lxd-container"]} == {Pool.LXD}

    def test_pool_reads_as_its_value(self):
        assert str(Pool.WSL) == "wsl"
        assert f"{Pool.CLOUD}" == "cloud"
        assert Pool.LXD == "lxd"


# Every pool's max jobs: what is accepted and what is refused.
MAX_JOBS_CASES = [
    pytest.param(Pool.LXD, 0, True, id="lxd-zero-disables"),
    pytest.param(Pool.LXD, 8, True, id="lxd-any-count"),
    pytest.param(Pool.CLOUD, 2, True, id="cloud-any-count"),
    pytest.param(Pool.WSL, 1, True, id="wsl-one"),
    pytest.param(Pool.WSL, 2, False, id="wsl-above-its-cap"),
    pytest.param(Pool.LXD, -1, False, id="negative"),
    pytest.param(Pool.LXD, True, False, id="a-bool-is-not-a-count"),
    pytest.param(Pool.LXD, "2", False, id="a-string-is-not-a-count"),
]


@pytest.mark.parametrize("pool, value, accepted", MAX_JOBS_CASES)
def test_max_jobs_error(pool, value, accepted):
    assert (max_jobs_error(pool, value) is None) is accepted
