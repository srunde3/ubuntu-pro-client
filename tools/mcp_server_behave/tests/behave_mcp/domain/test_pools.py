from behave_mcp.domain.pools import Pool, pools_of


class TestPools:
    def test_pools_of_collects_each_pool_once(self):
        assert pools_of(["lxd-vm", "lxd-container", "wsl"]) == {
            Pool.LXD,
            Pool.WSL,
        }

    def test_pro_fips_types_are_cloud(self):
        assert pools_of(["aws.pro-fips", "gcp.pro-fips"]) == {Pool.CLOUD}

    def test_pool_reads_as_its_value(self):
        assert str(Pool.WSL) == "wsl"
        assert f"{Pool.CLOUD}" == "cloud"
        assert Pool.LXD == "lxd"
