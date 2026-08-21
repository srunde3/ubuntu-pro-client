# Trusty VirtualBox UAT

End-to-end behave tests for `ubuntu-advantage-tools` on a real Ubuntu 14.04
(Trusty) VM, provisioned with Vagrant + VirtualBox. Dev-only; not packaged.

The VM is destroyed and recreated between feature files, not between scenarios.
Scenarios in one file share a machine and run top to bottom, so anything that
needs a pristine machine belongs in its own `.feature`.

## Setup

Requires `vagrant`, `VBoxManage`, and `uv` on the host, plus a locally built
Trusty `.deb` and a contract token that entitles ESM on Trusty.

```sh
export UACLIENT_UAT_DEB_PATH=/path/to/ubuntu-advantage-tools_19.7_amd64.deb
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
