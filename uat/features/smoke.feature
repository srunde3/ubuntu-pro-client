Feature: Trusty VirtualBox UAT harness

  Scenario: Execute a command on the Trusty VirtualBox SUT
    Given a `trusty` `vagrant` machine
    When I run `lsb_release -sc`
    Then the command succeeds
    And stdout contains `trusty`
