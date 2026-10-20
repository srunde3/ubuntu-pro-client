---
name: behave-mcp-integration-tests
description: What integration tests in tools/mcp_server_behave test, with good and bad examples. Covers MCP tools over the real protocol, server wiring and startup, the behave-campaign CLI, and the parser on the real features/ corpus. Use when writing or reviewing tests for an MCP tool, a CLI command, server.py wiring, startup config, or parsing of real feature files.
---

# behave MCP integration tests

An integration test proves that real components work together at a boundary a caller uses. The caller is an MCP client, a person at the CLI, or the feature files themselves. Everything inside the test process is real. Nothing starts tox or behave: a test that needs a job fakes `subprocess.Popen`, and a campaign test fakes the runner's lanes and ticker.

Every integration test carries the `integration` marker. A module of integration tests sets it once at the top, under its imports:

```python
pytestmark = pytest.mark.integration
```

The rules behind a result are already proven by unit tests. An integration test proves the boundary: the request is accepted, the right components are wired up, and the response or error reaches the caller in the shape the caller relies on.

## MCP tools: what a client sees

These tests call tools through `create_connected_server_and_client_session(mcp)`. They check the tool list, argument validation, the JSON payload, and the error text a client receives.

Good: one tool call, and assertions on the fields a client uses.

```python
async with create_connected_server_and_client_session(mcp) as client:
    await client.call_tool("create_campaign", {"campaign_id": CAMPAIGN_ID})
    result = await client.call_tool(
        "await_campaign_events", {"campaign_id": CAMPAIGN_ID}
    )

payload = result_json(result)
assert [e["kind"] for e in payload["events"]] == ["campaign.created"]
assert payload["next_seq"] == 1
```

Bad: calling the tool function in `server.py` directly. It skips argument parsing, the `@tool` serialisation and FastMCP's error handling, which are what this kind of test exists to cover.

```python
payload = server.await_campaign_events(campaign_id=CAMPAIGN_ID).model_dump()
```

Bad: re-proving scheduling rules through the protocol, one permutation per test. Each permutation belongs in a `plan_tick` unit test. One protocol test that shows the tool reports the result is enough.

Bad: a job test without a fake `Popen`. It starts a real `tox -e behave` and turns into an e2e test that no one meant to write.

## Server wiring and startup: how the process is assembled

Wiring tests check the objects `server.py` builds and shares, such as one event log shared by the runner and every service. Startup tests check that bad configuration ends the process with a clean message. Configuration is read at import, so these tests import the server in a fresh subprocess.

Good: the shared object is identified directly.

```python
service = fresh_server.campaign_service("")
runner = fresh_server.campaign_runner("")

assert service._events is runner._events
```

Bad: testing startup config by importing `behave_mcp.server` inside the test process. The module is already imported by `tests/conftest.py`, so the import does nothing and the test passes whatever the config.

## CLI: what a person sees

CLI tests run `behave_campaign.cli.main(argv)` over real files, and check the exit code, stdout JSON or CSV, and the stderr message.

Good: the exit code, the message a person reads, and proof that nothing was written.

```python
error = run(["record"], [unplanned], expect=2)

assert "unplanned units" in error
assert campaign_file.read_text() == original
```

Bad: calling `CampaignService` from a CLI test. That is a service unit test in the wrong file, and it never exercises argument parsing.

## Real corpus: the feature files as they are

These tests parse the repository's real `features/`. They check properties that must hold for every feature file: each one parses, scenarios have combos, config tags are read.

Good: an invariant that survives normal feature edits.

```python
assert len(details) == len(paths)
```

Bad: an exact count for a feature that changes often. It breaks when someone adds a release to an Examples table, and the failure says nothing about the parser.

```python
assert len(expired.combos) == 8
```

Run them with `uv run pytest -q -m "integration and not e2e"`.
