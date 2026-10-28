"""The service's own config for a checkout.

The behave option names are copied from ``features/environment.py``
(``UAClientBehaveConfig.all_options`` and ``redact_options``); add new ones
here by hand.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping

from pro_behave.jobs.domain.pools import DEFAULT_MAX_JOBS, Pool, max_jobs_error

BEHAVE_OPTIONS = (
    "collect_coverage",
    "destroy_instances",
    "ephemeral_instance",
    "snapshot_strategy",
    "sbuild_output_to_terminal",
    "cloud_credentials_path",
    "contract_token",
    "contract_token_legacy",
    "contract_token_staging",
    "contract_token_staging_expired",
    "contract_token_staging_expired_sometimes",
    "contract_staging_service_account_username",
    "contract_staging_service_account_password",
    "landscape_registration_key",
    "landscape_api_access_key",
    "landscape_api_secret_key",
    "machine_types",
    "releases",
    "private_key_file",
    "private_key_name",
    "artifact_dir",
    "install_from",
    "custom_ppa",
    "debs_path",
    "userdata_file",
    "check_version",
    "sbuild_chroot",
    "wsl_pubkey_path",
    "wsl_privkey_path",
    "wsl_ip_address",
)

SECRET_OPTIONS = frozenset(
    {
        "contract_token",
        "contract_token_legacy",
        "contract_token_staging",
        "contract_token_staging_expired",
        "contract_token_staging_expired_sometimes",
        "contract_staging_service_account_password",
        "landscape_registration_key",
        "landscape_api_access_key",
        "landscape_api_secret_key",
    }
)

# Each job sets these itself, from what it was asked to run.
JOB_OWNED_OPTIONS = frozenset({"install_from", "releases", "machine_types"})

# How a secret appears anywhere outside the config file.
SECRET_SHOWN = "<set>"


@dataclass(frozen=True)
class CheckoutConfig:
    """Max jobs per pool, and the behave options every job runs with."""

    max_jobs: Mapping[Pool, int] = field(
        default_factory=lambda: dict(DEFAULT_MAX_JOBS)
    )
    behave: Mapping[str, str] = field(default_factory=dict)


def behave_option_error(name: str) -> str | None:
    """Why ``name`` cannot be set in the config, or None if it can."""
    if name not in BEHAVE_OPTIONS:
        return f"unknown behave option {name!r}"
    if name in JOB_OWNED_OPTIONS:
        return (
            f"{name} is set per job, from what it was asked to run, so it "
            "cannot be configured"
        )
    return None


def parse_config(document: Mapping[str, Any]) -> CheckoutConfig:
    """A config from its stored form. Raises ValueError naming the problem."""
    max_jobs = dict(DEFAULT_MAX_JOBS)
    for pool_name, value in dict(document.get("max_jobs", {})).items():
        try:
            pool = Pool(pool_name)
        except ValueError:
            raise ValueError(f"unknown pool {pool_name!r} in max_jobs")
        error = max_jobs_error(pool, value)
        if error:
            raise ValueError(error)
        max_jobs[pool] = value
    behave = {}
    for name, value in dict(document.get("behave", {})).items():
        error = behave_option_error(name)
        if error:
            raise ValueError(error)
        if not isinstance(value, str):
            raise ValueError(f"behave option {name} must be a string")
        behave[name] = value
    return CheckoutConfig(max_jobs=max_jobs, behave=behave)


def config_document(config: CheckoutConfig) -> dict[str, Any]:
    """The stored form of ``config``."""
    return {
        "max_jobs": {
            pool.value: config.max_jobs.get(pool, 0) for pool in Pool
        },
        "behave": dict(sorted(config.behave.items())),
    }


def shown_options(behave: Mapping[str, str]) -> dict[str, str]:
    """The behave options as anything but the config file may show them."""
    return {
        name: SECRET_SHOWN if name in SECRET_OPTIONS else value
        for name, value in sorted(behave.items())
    }
