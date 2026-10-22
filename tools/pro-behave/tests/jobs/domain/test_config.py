"""The checkout config: what it accepts, and the environment it gives a job."""

import pytest

from pro_behave.jobs.domain.config import (
    BEHAVE_OPTIONS,
    JOB_OWNED_OPTIONS,
    SECRET_OPTIONS,
    SECRET_SHOWN,
    CheckoutConfig,
    behave_option_error,
    config_document,
    job_environment,
    parse_config,
    shown_options,
)
from pro_behave.jobs.domain.pools import DEFAULT_MAX_JOBS, Pool

TOKEN = "any-token"
PPA = "ppa:someone/something"


def test_secrets_and_job_owned_options_are_behave_options():
    assert SECRET_OPTIONS <= set(BEHAVE_OPTIONS)
    assert JOB_OWNED_OPTIONS <= set(BEHAVE_OPTIONS)


@pytest.mark.parametrize(
    "name, accepted",
    [
        pytest.param("custom_ppa", True, id="an-option"),
        pytest.param("contract_token", True, id="a-secret"),
        pytest.param("install_from", False, id="set-per-job"),
        pytest.param("releases", False, id="set-per-job-too"),
        pytest.param("no_such_option", False, id="unknown"),
    ],
)
def test_behave_option_error(name, accepted):
    assert (behave_option_error(name) is None) is accepted


def test_an_empty_document_is_the_defaults():
    config = parse_config({})

    assert config == CheckoutConfig(max_jobs=DEFAULT_MAX_JOBS, behave={})


def test_a_config_survives_its_stored_form():
    config = CheckoutConfig(
        max_jobs={Pool.LXD: 4, Pool.CLOUD: 2, Pool.WSL: 1},
        behave={"custom_ppa": PPA, "contract_token": TOKEN},
    )

    assert parse_config(config_document(config)) == config


@pytest.mark.parametrize(
    "document, message",
    [
        pytest.param({"max_jobs": {"moon": 1}}, "unknown pool", id="pool"),
        pytest.param({"max_jobs": {"wsl": 2}}, "wsl max jobs", id="cap"),
        pytest.param(
            {"behave": {"install_from": "proposed"}}, "set per job", id="owned"
        ),
        pytest.param({"behave": {"custom_ppa": 1}}, "string", id="type"),
    ],
)
def test_a_bad_document_is_refused_by_name(document, message):
    with pytest.raises(ValueError, match=message):
        parse_config(document)


def test_secrets_are_never_shown():
    shown = shown_options({"custom_ppa": PPA, "contract_token": TOKEN})

    assert shown == {"contract_token": SECRET_SHOWN, "custom_ppa": PPA}


class TestJobEnvironment:
    def test_nothing_behave_reads_is_inherited(self):
        inherited = {"PATH": "/bin", "UACLIENT_BEHAVE_CONTRACT_TOKEN": TOKEN}

        env = job_environment(inherited, CheckoutConfig(), "local")

        assert "UACLIENT_BEHAVE_CONTRACT_TOKEN" not in env
        assert env["PATH"] == "/bin"

    def test_every_configured_option_is_passed(self):
        config = CheckoutConfig(behave={"contract_token": TOKEN})

        env = job_environment({}, config, "local")

        assert env["UACLIENT_BEHAVE_CONTRACT_TOKEN"] == TOKEN

    def test_the_install_source_is_the_job_s_own(self):
        inherited = {"UACLIENT_BEHAVE_INSTALL_FROM": "stable"}

        env = job_environment(inherited, CheckoutConfig(), "proposed")

        assert env["UACLIENT_BEHAVE_INSTALL_FROM"] == "proposed"
