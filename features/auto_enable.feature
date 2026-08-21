Feature: Trusty attach with auto-enable

  Attaching without `--no-auto-enable` enables entitled services unattended, so
  apt credentials are validated and written without any explicit enable call.

  Background:
    Given a `trusty` `vagrant` machine with ubuntu-pro-client installed

  Scenario: Attaching auto-enables esm-infra and configures apt
    When I attach using the configured contract token with auto-enable
    Then the command succeeds
    And service `esm-infra` is enabled
    Then the apt auth file exists
    And the apt auth file contains `machine esm.ubuntu.com/ login bearer`
    Then the file `/etc/apt/sources.list.d/ubuntu-esm-infra-trusty.list` exists
    And the file `/etc/apt/trusted.gpg.d/ubuntu-advantage-esm-infra-trusty.gpg` exists

  Scenario: The auto-enabled repository is usable
    When I run `apt-get update -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu-esm-infra-trusty.list -o Dir::Etc::sourceparts=/dev/null -o APT::Get::List-Cleanup=0` as sudo
    Then the command succeeds
    When I run `apt-get install --only-upgrade --assume-yes curl -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu-esm-infra-trusty.list -o Dir::Etc::sourceparts=/dev/null` as sudo
    Then the command succeeds
    And the installed version of `curl` comes from `esm.ubuntu.com`
