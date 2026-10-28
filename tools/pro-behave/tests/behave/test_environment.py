"""The environment a job gives behave."""

from pro_behave.behave.environment import job_environment

TOKEN = "any-token"


class TestJobEnvironment:
    def test_nothing_behave_reads_is_inherited(self):
        inherited = {"PATH": "/bin", "UACLIENT_BEHAVE_CONTRACT_TOKEN": TOKEN}

        env = job_environment(inherited, {}, "local")

        assert "UACLIENT_BEHAVE_CONTRACT_TOKEN" not in env
        assert env["PATH"] == "/bin"

    def test_every_configured_option_is_passed(self):
        options = {"contract_token": TOKEN}

        env = job_environment({}, options, "local")

        assert env["UACLIENT_BEHAVE_CONTRACT_TOKEN"] == TOKEN

    def test_the_install_source_is_the_job_s_own(self):
        inherited = {"UACLIENT_BEHAVE_INSTALL_FROM": "stable"}

        env = job_environment(inherited, {}, "proposed")

        assert env["UACLIENT_BEHAVE_INSTALL_FROM"] == "proposed"
