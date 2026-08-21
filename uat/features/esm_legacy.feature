Feature: Trusty ESM Infra Legacy

  Background:
    Given a `trusty` `vagrant` machine with ubuntu-pro-client installed

  Scenario: Enable esm-infra and esm-infra-legacy on Trusty
    When I attach using the configured contract token
    Then the command succeeds
    When I run `ua enable esm-infra` as sudo
    Then the command succeeds
    And service `esm-infra` is enabled
    When I run `ua enable esm-infra-legacy` as sudo
    Then the command succeeds
    And service `esm-infra-legacy` is enabled
    Then the file `/etc/apt/sources.list.d/ubuntu-esm-infra-trusty.list` exists
    And the file `/etc/apt/trusted.gpg.d/ubuntu-advantage-esm-infra-trusty.gpg` exists
    Then the file `/etc/apt/sources.list.d/ubuntu-esm-infra-legacy-trusty.list` exists
    And the file `/etc/apt/sources.list.d/ubuntu-esm-infra-legacy-trusty.list` contains `esm.ubuntu.com/infra-legacy/ubuntu`
    Then the file `/etc/apt/trusted.gpg.d/ubuntu-advantage-esm-infra-legacy-trusty.gpg` exists
    Then the apt auth file exists
    And the apt auth file contains `machine esm.ubuntu.com/ login bearer`
    And the apt auth file contains `machine esm.ubuntu.com/infra-legacy/ login bearer`

  Scenario: Both ESM repositories are reachable with the configured credentials
    When I run `apt-get update -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu-esm-infra-trusty.list -o Dir::Etc::sourceparts=/dev/null -o APT::Get::List-Cleanup=0` as sudo
    Then the command succeeds
    When I run `apt-get update -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu-esm-infra-legacy-trusty.list -o Dir::Etc::sourceparts=/dev/null -o APT::Get::List-Cleanup=0` as sudo
    Then the command succeeds
    And apt policy contains origin `UbuntuESM`
