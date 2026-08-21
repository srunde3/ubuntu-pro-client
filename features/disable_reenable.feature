Feature: Trusty ESM disable and re-enable

  Background:
    Given a `trusty` `vagrant` machine with ubuntu-pro-client installed

  # This was shown to be the case in 19.7
  Scenario: Disabling esm-infra keeps the lists and credentials
    When I attach using the configured contract token
    Then the command succeeds
    When I run `ua enable esm-infra` as sudo
    Then the command succeeds
    And the apt auth file contains `machine esm.ubuntu.com/ login bearer`
    When I run `ua disable esm-infra` as sudo
    Then the command succeeds
    And service `esm-infra` is disabled
    And the apt auth file contains `machine esm.ubuntu.com/ login bearer`
    Then the file `/etc/apt/sources.list.d/ubuntu-esm-infra-trusty.list` exists
    And the file `/etc/apt/trusted.gpg.d/ubuntu-advantage-esm-infra-trusty.gpg` exists

  Scenario: Re-enabling esm-infra revalidates and restores the credentials
    When I run `ua enable esm-infra` as sudo
    Then the command succeeds
    And service `esm-infra` is enabled
    And the apt auth file contains `machine esm.ubuntu.com/ login bearer`
    When I run `apt-get update -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu-esm-infra-trusty.list -o Dir::Etc::sourceparts=/dev/null -o APT::Get::List-Cleanup=0` as sudo
    Then the command succeeds

  Scenario: Disabling esm-infra-legacy removes its apt configuration entirely
    When I run `ua enable esm-infra-legacy` as sudo
    Then the command succeeds
    And service `esm-infra-legacy` is enabled
    And the apt auth file contains `machine esm.ubuntu.com/infra-legacy/ login bearer`
    When I run `ua disable esm-infra-legacy` as sudo
    Then the command succeeds
    And service `esm-infra-legacy` is disabled
    And the apt auth file does not contain `machine esm.ubuntu.com/infra-legacy/ login bearer`
    Then the file `/etc/apt/sources.list.d/ubuntu-esm-infra-legacy-trusty.list` does not exist
    And the file `/etc/apt/trusted.gpg.d/ubuntu-advantage-esm-infra-legacy-trusty.gpg` does not exist
