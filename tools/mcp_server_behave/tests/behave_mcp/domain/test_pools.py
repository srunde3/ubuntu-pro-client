from behave_mcp.domain.pools import POOL_OF, Pool


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
