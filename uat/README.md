# Features

End-to-end `behave` tests for `ubuntu-advantage-tools` on Trusty.

These e2e tests were created well after the last release of Ubuntu 14.04 Trusty
in order to support security patches until Trusty reaches the end of
legacy support. They are conceptually similar to what is in `main` but run
a smaller set of features and have more limited options.

These run using modern Python (`uv`) because they do not ship with the Trusty
package, which must run on Python 3.4.

## Setup

Requires `vagrant`, `VBoxManage`, and `uv` on the host, plus a contract token
that entitles ESM Legacy on Trusty. These tests are primarily focused on
ensuring access to ESM legacy.

```sh
export UACLIENT_UAT_CONTRACT_TOKEN='...'
```

The client under test is installed either from a locally built package or from
the archive. `UACLIENT_UAT_INSTALL_FROM` selects which, defaulting to `prebuilt`
when `UACLIENT_UAT_DEB_PATH` is set and `archive` otherwise.

```sh
export UACLIENT_UAT_INSTALL_FROM=prebuilt
export UACLIENT_UAT_DEB_PATH=/path/to/ubuntu-advantage-tools_<version>_amd64.deb

export UACLIENT_UAT_INSTALL_FROM=archive   # installs ubuntu-advantage-tools with apt
```

## Running

```sh
uv run behave                            # whole suite
uv run behave features/attach.feature    # one feature
uv run behave --dry-run --no-summary     # step wiring check, no VM
```

Only one behave process may drive the Vagrantfile at a time.

## Debugging

`UACLIENT_UAT_KEEP_VM=1` skips teardown, leaving the machine running so you can
inspect it once the run finishes:

```sh
UACLIENT_UAT_KEEP_VM=1 uv run behave features/attach.feature
vagrant ssh
```

This only suppresses teardown *after* a run. The next behave run destroys the
machine before its first feature regardless, so inspect before starting another.
Tear it down by hand with `vagrant destroy --force`.
