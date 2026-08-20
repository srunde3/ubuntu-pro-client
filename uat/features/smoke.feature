Feature: Trusty VirtualBox UAT harness

  Scenario: Execute a command on the Trusty VirtualBox SUT
    Given a `trusty` `vagrant` machine with ubuntu-pro-client installed
    When I run `lsb_release -sc`
    Then the command succeeds
    And stdout contains `trusty`

  Scenario: Transfer a file through the Trusty VirtualBox SUT
    Given a `trusty` `vagrant` machine with ubuntu-pro-client installed
    When I transfer a file containing `uat-round-trip` through the machine
    Then the transferred file contains `uat-round-trip`

  Scenario: Install the supplied client package on Trusty
    Given a `trusty` `vagrant` machine with ubuntu-pro-client installed
    When I run `ua version`
    Then the command succeeds

  Scenario: Execute a privileged command on the Trusty SUT
    Given a `trusty` `vagrant` machine with ubuntu-pro-client installed
    When I run `id -u` as sudo
    Then the command succeeds
    And stdout contains `0`

