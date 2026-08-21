Feature: Trusty ESM attachment

  Background:
    Given a `trusty` `vagrant` machine with ubuntu-pro-client installed

  Scenario: Attach and enable ESM infrastructure on Trusty
    When I attach using the configured contract token
    Then the command succeeds
    When I run `ua enable esm-infra` as sudo
    Then the command succeeds
    And service `esm-infra` is enabled
    Then the apt auth file exists
    And the apt auth file contains `machine esm.ubuntu.com/`
    Then the file `/etc/apt/sources.list.d/ubuntu-esm-infra-trusty.list` exists
    Then the file `/etc/apt/trusted.gpg.d/ubuntu-advantage-esm-infra-trusty.gpg` exists