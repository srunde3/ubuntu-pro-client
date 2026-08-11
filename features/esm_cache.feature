Feature: ESM cache behavior

  Scenario Outline: esm cache failures don't generate errors
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    When I disable access to esm.ubuntu.com
    And I apt update
    # Wait for the hook to fail
    When I wait `5` seconds
    And I run `systemctl --failed` with sudo
    Then stdout does not match regexp:
      """
      esm-cache\.service
      """
    When I run `journalctl -o cat -u esm-cache.service` with sudo
    Then stdout does not contain substring:
      """
      raise FetchFailedException()
      """
    Then stdout matches regexp:
      """
      "WARNING", "ubuntupro.apt", "fail", \d+, "Failed to fetch ESM Apt Cache item: https://esm.ubuntu.com/apps/ubuntu/dists/<release>-apps-security/InRelease", {}]
      """
    When I run `ls /var/crash/` with sudo
    Then stdout does not contain substring:
      """
      _usr_lib_ubuntu-advantage_esm_cache
      """

    @releases:lts_supported
    @releases:lts_esm
    @machine_types:lxd-container
    Examples: ubuntu release
      | release  | machine_type  |
      | bionic   | lxd-container |
      | focal    | lxd-container |
      | jammy    | lxd-container |
      | noble    | lxd-container |
      | resolute | lxd-container |

  # Duplicating just for xenial so we disable it on GH
  # See GH: #3013
  @no_gh
  Scenario Outline: esm cache failures don't generate errors on xenial
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    When I disable access to esm.ubuntu.com
    And I apt update
    # Wait for the hook to fail
    When I wait `5` seconds
    And I run `systemctl --failed` with sudo
    Then stdout does not match regexp:
      """
      esm-cache\.service
      """
    When I run `journalctl -o cat -u esm-cache.service` with sudo
    Then stdout does not contain substring:
      """
      raise FetchFailedException()
      """
    Then stdout matches regexp:
      """
      "WARNING", "ubuntupro.apt", "fail", \d+, "Failed to fetch ESM Apt Cache item: https://esm.ubuntu.com/apps/ubuntu/dists/<release>-apps-security/InRelease", {}]
      """
    When I run `ls /var/crash/` with sudo
    Then stdout does not contain substring:
      """
      _usr_lib_ubuntu-advantage_esm_cache
      """

    # This test uses release xenial only. It exists to cover xenial by
    # itself. The scenario must run with the tag @no_gh. The test does
    # not need to run on every release.
    @releases:fixed
    Examples: ubuntu release
      | release | machine_type  |
      | xenial  | lxd-container |
