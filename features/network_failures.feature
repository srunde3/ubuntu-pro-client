@uses.config.contract_token
Feature: Ensure network errors are handled gracefully across various services

  Scenario Outline: Various HTTP errors are handled gracefully on attaching contract token
    # This test simulates various HTTP errors by mocking the response from the serviceclient
    # when trying to attach contract token
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    # 400 Bad Request
    When I create a response overlay for `/v1/context/machines/token` with response code `400` and error message `Bad Request`
    And I append the following on uaclient config:
      """
      features:
        serviceclient_url_responses: "/tmp/response-overlay.json"
      """
    When I attempt to attach `contract_token` with sudo
    Then stderr contains substring:
      """
      Error connecting to /v1/context/machines/token: 400 {"error": "Bad Request"}
      """
    Then the machine is unattached
    # 404 Not Found
    When I create a response overlay for `/v1/context/machines/token` with response code `404` and error message `Not Found`
    And I append the following on uaclient config:
      """
      features:
        serviceclient_url_responses: "/tmp/response-overlay.json"
      """
    When I attempt to attach `contract_token` with sudo
    Then stderr contains substring:
      """
      Error connecting to /v1/context/machines/token: 404 {"error": "Not Found"}
      """
    Then the machine is unattached
    # 503 Bad Gateway
    When I create a response overlay for `/v1/context/machines/token` with response code `503` and error message `Bad Gateway`
    And I append the following on uaclient config:
      """
      features:
        serviceclient_url_responses: "/tmp/response-overlay.json"
      """
    When I attempt to attach `contract_token` with sudo
    Then stderr contains substring:
      """
      Error connecting to /v1/context/machines/token: 503 {"error": "Bad Gateway"}
      """
    Then the machine is unattached

    # This test uses release xenial only, by design. It also checks this
    # behavior on a legacy release. The newest LTS release is tested below.
    # It does not need to run on every release.
    @releases:fixed
    Examples: xenial
      | release | machine_type  |
      | xenial  | lxd-container |

    # This table always uses the newest LTS release. Update the row in
    # place when a new LTS release ships. Do not add another row.
    @releases:latest_lts
    Examples: latest lts
      | release  | machine_type  |
      | resolute | lxd-container |

  Scenario Outline: Network errors for attaching contract token are handled gracefully
    # This test simulates network failure by disabling internet connection
    # and then trying to attach contract token
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    When I disable any internet connection on the machine
    And I attempt to attach `contract_token` with sudo
    Then stderr contains substring:
      """
      Failed to attach machine. See https://ubuntu.com/pro/dashboard
      """
    Then the machine is unattached

    # This test uses release xenial only, by design. It also checks this
    # behavior on a legacy release. The newest LTS release is tested below.
    # It does not need to run on every release.
    @releases:fixed
    Examples: xenial
      | release | machine_type  |
      | xenial  | lxd-container |

    # This table always uses the newest LTS release. Update the row in
    # place when a new LTS release ships. Do not add another row.
    @releases:latest_lts
    Examples: latest lts
      | release  | machine_type  |
      | resolute | lxd-container |

  Scenario Outline: Network errors for enabling Realtime kernel and Livepatch are handled gracefully
    # This test simulates network failure by disabling internet connection
    # and then trying to enable realtime-kernel or livepatch
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    When I attach `contract_token` with sudo and options `--no-auto-enable`
    Then the machine is attached
    Then I verify that `<service>` is disabled
    When I disable any internet connection on the machine
    And I verify that running `pro enable <service> --assume-yes` `with sudo` exits `1`
    Then stderr contains substring:
      """
      Failed to connect to https://contracts.canonical.com/v1/contracts/
      """
    Then I verify that `<service>` is disabled

    # Realtime kernel does not work on LXD containers. This test uses a VM
    # instead. From release resolute onward, realtime-kernel is in the
    # archives. Pro no longer manages it there. This coverage stays fixed
    # at releases xenial and noble, by design. It does not need to run on
    # every release.
    @releases:fixed
    Examples: realtime-kernel
      | release | machine_type | service         |
      | xenial  | lxd-vm       | realtime-kernel |
      | noble   | lxd-vm       | realtime-kernel |

    # This test uses release xenial only, by design. It also checks this
    # behavior on a legacy release. The newest LTS release is tested below.
    # It does not need to run on every release.
    @releases:fixed
    Examples: livepatch xenial
      | release | machine_type  | service   |
      | xenial  | lxd-container | livepatch |

    # This table always uses the newest LTS release. Update the row in
    # place when a new LTS release ships. Do not add another row.
    @releases:latest_lts
    Examples: livepatch latest lts
      | release  | machine_type  | service   |
      | resolute | lxd-container | livepatch |
