# Features

End-to-end `behave` tests for `ubuntu-advantage-tools` on Trusty.

These e2e tests were created well after the last release of Ubuntu 14.04 Trusty
in order to support security patches until Trusty reaches the end of
legacy support. They are conceptually similar to what is in `main` but run
a smaller set of features.

These run using modern Python (`uv`) because they do not ship with the Trusty
package, which must run on Python 3.4.

## Setup

Requires `vagrant`, `VBoxManage`, and `uv` on the host, plus a locally built
Trusty `.deb` and a contract token that entitles ESM Legacy on Trusty.

```sh
export UACLIENT_UAT_DEB_PATH=/path/to/ubuntu-advantage-tools_<version>_amd64.deb
export UACLIENT_UAT_CONTRACT_TOKEN='...'
```

Set `UACLIENT_UAT_KEEP_VM=1` to skip teardown when debugging.

## Running

```sh
uv run behave                            # whole suite
uv run behave features/attach.feature    # one feature
uv run behave --dry-run --no-summary     # step wiring check, no VM
```

Only one behave process may drive the Vagrantfile at a time.
