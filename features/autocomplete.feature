Feature: Pro autocomplete commands

  # Side effect: this verifies that `ua` still works as a command
  Scenario Outline: Verify autocomplete options
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    When I prepare the autocomplete test
    And I press tab twice to autocomplete the `ua` command
    Then stdout matches regexp:
      """
      --debug         +auto-attach     +detach          +refresh\r
      --help          +collect-logs    +disable         +security-status\r
      --version       +config          +enable          +status\r
      api             +cve             +fix             +system\r
      attach          +cves            +help            +version
      """
    When I press tab twice to autocomplete the `pro` command
    Then stdout matches regexp:
      """
      --debug         +auto-attach     +detach          +refresh\r
      --help          +collect-logs    +disable         +security-status\r
      --version       +config          +enable          +status\r
      api             +cve             +fix             +system\r
      attach          +cves            +help            +version
      """
    When I press tab twice to autocomplete the `ua enable` command
    Then stdout matches regexp:
      """
      anbox-cloud +esm-apps-legacy  +fips-updates      ros\s*
      cc-eal      +esm-infra        +landscape         ros-updates\s*
      cis         +esm-infra-legacy +livepatch         usg\s*
      esm-apps    +fips             +realtime-kernel\s*
      """
    When I press tab twice to autocomplete the `pro enable` command
    Then stdout matches regexp:
      """
      anbox-cloud +esm-apps-legacy  +fips-updates      ros\s*
      cc-eal      +esm-infra        +landscape         ros-updates\s*
      cis         +esm-infra-legacy +livepatch         usg\s*
      esm-apps    +fips             +realtime-kernel\s*
      """
    When I press tab twice to autocomplete the `ua disable` command
    Then stdout matches regexp:
      """
      anbox-cloud +esm-apps-legacy  +fips-updates      ros\s*
      cc-eal      +esm-infra        +landscape         ros-updates\s*
      cis         +esm-infra-legacy +livepatch         usg\s*
      esm-apps    +fips             +realtime-kernel\s*
      """
    When I press tab twice to autocomplete the `pro disable` command
    Then stdout matches regexp:
      """
      anbox-cloud +esm-apps-legacy  +fips-updates      ros\s*
      cc-eal      +esm-infra        +landscape         ros-updates\s*
      cis         +esm-infra-legacy +livepatch         usg\s*
      esm-apps    +fips             +realtime-kernel\s*
      """

    @releases:lts_supported
    @releases:lts_esm
    @releases:interim
    @machine_types:lxd-container
    # This skips xenial. On xenial, bash sorts the autocomplete output
    # in a different order. The stdout regexp checks cannot rely on a
    # fixed order there.
    @releases:skip:xenial
    Examples: ubuntu release
      | release  | machine_type  |
      | bionic   | lxd-container |
      | focal    | lxd-container |
      | jammy    | lxd-container |
      | noble    | lxd-container |
      | questing | lxd-container |
      | resolute | lxd-container |
