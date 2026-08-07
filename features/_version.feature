Feature: Pro is expected version

  @uses.config.check_version
  Scenario Outline: Check pro version
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    When I run `dpkg-query --showformat='${Version}' --show ubuntu-pro-client` with sudo
    Then I will see the following on stdout:
      """
      $behave_var{version}
      """
    When I run `pro version` with sudo
    Then I will see the following on stdout:
      """
      $behave_var{version}
      """
    # The following doesn't actually assert anything. It merely ensures that the output of
    # apt-cache policy ubuntu-pro-client on the test machine is included in our test output.
    # This is useful to manually verify the package is installed from the correct source e.g. -proposed.
    When I check the apt-cache policy of ubuntu-pro-client
    Then the apt-cache policy of ubuntu-pro-client is
      """
      THIS GETS REPLACED AT RUNTIME VIA A HACK IN steps/ubuntu_advantage_tools.py
      """

    @releases.lts.supported
    @releases.lts.esm
    @releases.interim.supported
    Examples: standard
      | release  | machine_type  |
      | xenial   | lxd-container |
      | xenial   | lxd-vm        |
      | xenial   | aws.generic   |
      | xenial   | azure.generic |
      | xenial   | gcp.generic   |
      | bionic   | lxd-container |
      | bionic   | lxd-vm        |
      | bionic   | aws.generic   |
      | bionic   | azure.generic |
      | bionic   | gcp.generic   |
      | bionic   | wsl           |
      | focal    | lxd-container |
      | focal    | lxd-vm        |
      | focal    | aws.generic   |
      | focal    | azure.generic |
      | focal    | gcp.generic   |
      | focal    | wsl           |
      | jammy    | lxd-container |
      | jammy    | lxd-vm        |
      | jammy    | aws.generic   |
      | jammy    | azure.generic |
      | jammy    | gcp.generic   |
      | jammy    | wsl           |
      | noble    | lxd-container |
      | noble    | lxd-vm        |
      | noble    | aws.generic   |
      | noble    | azure.generic |
      | noble    | gcp.generic   |
      | questing | lxd-container |
      | questing | lxd-vm        |
      | questing | aws.generic   |
      | questing | azure.generic |
      | questing | gcp.generic   |
      | resolute | lxd-container |
      | resolute | lxd-vm        |
      | resolute | aws.generic   |
      | resolute | azure.generic |
      | resolute | gcp.generic   |

    @releases.lts.supported
    @releases.lts.esm
    Examples: clouds
      | release  | machine_type   |
      | xenial   | aws.pro        |
      | xenial   | aws.pro-fips   |
      | xenial   | azure.pro      |
      | xenial   | azure.pro-fips |
      | xenial   | gcp.pro        |
      | bionic   | aws.pro        |
      | bionic   | aws.pro-fips   |
      | bionic   | azure.pro      |
      | bionic   | azure.pro-fips |
      | bionic   | gcp.pro        |
      | bionic   | gcp.pro-fips   |
      | focal    | aws.pro        |
      | focal    | aws.pro-fips   |
      | focal    | azure.pro      |
      | focal    | azure.pro-fips |
      | focal    | gcp.pro        |
      | focal    | gcp.pro-fips   |
      | jammy    | aws.pro        |
      | jammy    | azure.pro      |
      | jammy    | gcp.pro        |
      | noble    | aws.pro        |
      | noble    | azure.pro      |
      | noble    | gcp.pro        |
      | resolute | aws.pro        |
      | resolute | azure.pro      |
      | resolute | gcp.pro        |

  @uses.config.check_version
  @upgrade
  Scenario Outline: Check pro version
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    When I run `dpkg-query --showformat='${Version}' --show ubuntu-pro-client` with sudo
    Then I will see the following on stdout:
      """
      $behave_var{version}
      """
    When I run `pro version` with sudo
    Then I will see the following on stdout:
      """
      $behave_var{version}
      """
    # The following doesn't actually assert anything. It merely ensures that the output of
    # apt-cache policy ubuntu-pro-client on the test machine is included in our test output.
    # This is useful to manually verify the package is installed from the correct source e.g. -proposed.
    When I check the apt-cache policy of ubuntu-pro-client
    Then the apt-cache policy of ubuntu-pro-client is
      """
      THIS GETS REPLACED AT RUNTIME VIA A HACK IN steps/ubuntu_advantage_tools.py
      """

    @releases.lts.supported
    @releases.lts.esm
    @releases.interim.supported
    Examples: version
      | release  | machine_type  |
      | xenial   | lxd-container |
      | bionic   | lxd-container |
      | focal    | lxd-container |
      | jammy    | lxd-container |
      | noble    | lxd-container |
      | questing | lxd-container |
      | resolute | lxd-container |

  @uses.config.contract_token
  Scenario Outline: Attached show version in a ubuntu machine
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    When I attach `contract_token` with sudo
    And I run `pro version` as non-root
    Then I will see the uaclient version on stdout
    When I run `pro version` with sudo
    Then I will see the uaclient version on stdout
    When I run `pro --version` as non-root
    Then I will see the uaclient version on stdout
    When I run `pro --version` with sudo
    Then I will see the uaclient version on stdout

    @releases.lts.supported
    @releases.lts.esm
    @releases.interim.supported
    Examples: ubuntu release
      | release  | machine_type  |
      | bionic   | lxd-container |
      | focal    | lxd-container |
      | xenial   | lxd-container |
      | jammy    | lxd-container |
      | noble    | lxd-container |
      | questing | lxd-container |
      | resolute | lxd-container |

  @arm64
  Scenario Outline: Check for newer versions of the client in an ubuntu machine
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    # Make sure we have a fresh, just rebooted, environment
    When I reboot the machine
    Then I verify that no files exist matching `/run/ubuntu-advantage/candidate-version`
    When I run `pro status` with sudo
    Then stderr does not match regexp:
      """
      .*\[info\].* A new version is available: 2:99.9.9
      Please run:
          sudo apt install ubuntu-pro-client
      to get the latest bug fixes and new features.
      """
    And I verify that files exist matching `/run/ubuntu-advantage/candidate-version`
    # We forge a candidate to see results
    When I delete the file `/run/ubuntu-advantage/candidate-version`
    And I create the file `/run/ubuntu-advantage/candidate-version` with the following:
      """
      2:99.9.9
      """
    And I run `pro status` as non-root
    Then stderr matches regexp:
      """
      .*\[info\].* A new version is available: 2:99.9.9
      Please run:
          sudo apt install ubuntu-pro-client
      to get the latest bug fixes and new features.
      """
    When I run `pro status --format json` as non-root
    Then stderr does not match regexp:
      """
      .*\[info\].* A new version is available: 2:99.9.9
      Please run:
          sudo apt install ubuntu-pro-client
      to get the latest bug fixes and new features.
      """
    When I run `pro config show` as non-root
    Then stderr matches regexp:
      """
      .*\[info\].* A new version is available: 2:99.9.9
      Please run:
          sudo apt install ubuntu-pro-client
      to get the latest bug fixes and new features.
      """
    When I run `pro api u.pro.version.v1` as non-root
    Then stdout matches regexp:
      """
      \"code\": \"new-version-available\"
      """
    When I verify that running `pro api u.pro.version.inexistent` `as non-root` exits `1`
    Then stdout matches regexp:
      """
      \"code\": \"new-version-available\"
      """
    When I run `pro api u.pro.version.v1` as non-root
    Then stderr does not match regexp:
      """
      .*\[info\].* A new version is available: 2:99.9.9
      Please run:
          sudo apt install ubuntu-pro-client
      to get the latest bug fixes and new features.
      """
    When I apt update
    # The update will bring a new candidate, which is the current installed version
    And I run `pro status` as non-root
    Then stderr does not match regexp:
      """
      .*\[info\].* A new version is available: 2:99.9.9
      Please run:
          sudo apt install ubuntu-pro-client
      to get the latest bug fixes and new features.
      """

    @releases.lts.supported
    @releases.lts.esm
    @releases.interim.supported
    Examples: ubuntu release
      | release  | machine_type  |
      | xenial   | lxd-container |
      | bionic   | lxd-container |
      | focal    | lxd-container |
      | jammy    | lxd-container |
      | noble    | lxd-container |
      | questing | lxd-container |
      | resolute | lxd-container |
