Feature: Trusty ESM refresh

  Background:
    Given a `trusty` `vagrant` machine with ubuntu-pro-client installed
    When I attach using the configured contract token
    Then the command succeeds
    When I run `ua enable esm-infra` as sudo
    Then the command succeeds

  Scenario: Refresh an attached Trusty ESM machine
    When I run `ua refresh` as sudo
    Then the command succeeds
    And service `esm-infra` is enabled