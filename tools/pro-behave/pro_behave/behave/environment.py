"""How a job's run options reach behave: ``UACLIENT_BEHAVE_*`` variables."""

from typing import Mapping

# features/environment.py reads UAClientBehaveConfig's options from
# variables with this prefix.
BEHAVE_ENV_PREFIX = "UACLIENT_BEHAVE_"

# The install source, which each job sets from its request. behave
# defaults it to 'local' when unset, but it is always set so other values
# (e.g. 'proposed') reach the run.
INSTALL_FROM_ENV_VAR = BEHAVE_ENV_PREFIX + "INSTALL_FROM"


def job_environment(
    inherited: Mapping[str, str],
    options: Mapping[str, str],
    install_from: str,
) -> dict[str, str]:
    """The environment a job runs with.

    Nothing behave reads comes from the process starting the job: every
    inherited ``UACLIENT_BEHAVE_*`` variable is dropped, so the job is the
    same whoever starts it.
    """
    env = {
        name: value
        for name, value in inherited.items()
        if not name.startswith(BEHAVE_ENV_PREFIX)
    }
    for name, value in options.items():
        env[BEHAVE_ENV_PREFIX + name.upper()] = value
    env[INSTALL_FROM_ENV_VAR] = install_from
    return env
