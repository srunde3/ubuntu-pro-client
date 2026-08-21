Feature: Trusty ESM detach

  Background:
    Given a `trusty` `vagrant` machine with ubuntu-pro-client installed

  Scenario: Detaching removes the apt credentials and reports the machine unattached
    When I attach using the configured contract token
    Then the command succeeds
    When I run `ua enable esm-infra` as sudo
    Then the command succeeds
    And the apt auth file contains `machine esm.ubuntu.com/ login bearer`
    When I run `echo y | ua detach` as sudo
    Then the command succeeds
    And stdout contains `This machine is now detached`
    When I run `ua status` as sudo
    Then the command succeeds
    And stdout contains `This machine is not attached to a UA subscription.`

  Scenario: Re-attaching and enabling revalidates the credentials
    When I attach using the configured contract token
    Then the command succeeds
    When I run `ua enable esm-infra` as sudo
    Then the command succeeds
    And service `esm-infra` is enabled
    And the apt auth file contains `machine esm.ubuntu.com/ login bearer`
    When I run `apt-get update -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu-esm-infra-trusty.list -o Dir::Etc::sourceparts=/dev/null -o APT::Get::List-Cleanup=0` as sudo
    Then the command succeeds
