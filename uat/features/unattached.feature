Feature: Command behaviour when unattached

  Ported from the repository's unattached_commands and unattached_status
  features so this harness does not lose that coverage.

  Background:
    Given a `trusty` `vagrant` machine with ubuntu-pro-client installed

  Scenario: Unattached status
    When I run `ua status` as non-root
    Then I will see the following on stdout:
      """
      SERVICE           AVAILABLE  DESCRIPTION
      cc-eal            no         Common Criteria EAL2 Provisioning Packages
      esm-infra         yes        UA Infra: Extended Security Maintenance
      esm-infra-legacy  yes        Expanded Security Maintenance for Infrastructure on Legacy Instances
      fips              no         NIST-certified FIPS modules
      fips-updates      no         Uncertified security updates to FIPS modules
      livepatch         yes        Canonical Livepatch service

      This machine is not attached to a UA subscription.
      See https://ubuntu.com/advantage
      """

  Scenario: Unattached detach
    When I run `ua detach` as non-root
    Then I will see the following on stderr:
      """
      This machine is not attached to a UA subscription.
      See https://ubuntu.com/advantage
      """

  Scenario: Unattached refresh
    When I run `ua refresh` as non-root
    Then I will see the following on stderr:
      """
      This machine is not attached to a UA subscription.
      See https://ubuntu.com/advantage
      """

  Scenario: Unattached enable of a known service
    When I run `ua enable livepatch` as non-root
    Then I will see the following on stderr:
      """
      To use 'livepatch' you need an Ubuntu Advantage subscription.
      Personal and community subscriptions are available at no charge
      See https://ubuntu.com/advantage
      """

  Scenario: Unattached enable of an unknown service
    When I run `ua enable foobar` as non-root
    Then I will see the following on stderr:
      """
      Cannot enable 'foobar'
      For a list of services see: sudo ua status
      """

  Scenario: Unattached disable of a known service
    When I run `ua disable livepatch` as non-root
    Then I will see the following on stderr:
      """
      To use 'livepatch' you need an Ubuntu Advantage subscription.
      Personal and community subscriptions are available at no charge
      See https://ubuntu.com/advantage
      """

  Scenario: Unattached disable of an unknown service
    When I run `ua disable foobar` as non-root
    Then I will see the following on stderr:
      """
      Cannot disable 'foobar'
      For a list of services see: sudo ua status
      """
