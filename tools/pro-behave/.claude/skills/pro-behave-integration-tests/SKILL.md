---
name: pro-behave-integration-tests
description: What integration tests in tools/pro-behave test, with good and bad examples. Covers MCP tools over the real protocol, server wiring and startup, the pro-behave CLI, and the parser on the real features/ corpus. Use when writing or reviewing tests for an MCP tool, a CLI command, server.py wiring, startup config, or parsing of real feature files.
---

# pro-behave integration tests

An integration test proves that real components work together at a boundary a caller uses. The caller is an MCP client, a person at the CLI, or the feature files themselves. Everything inside the test process is real. Nothing starts tox. A job tool test replaces `server._service` with `make_service` over a `FakeJobLauncher`, and a campaign tool test replaces `server.campaign_jobs` with a `FakeJobRunner`.

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

Bad: re-proving scheduling rules through the protocol, one permutation per test. Each permutation belongs in a `plan_campaign` unit test. One protocol test that shows the tool reports the result is enough.

Bad: a job test without a `FakeJobLauncher`. It starts a real `tox -e behave` and turns into an e2e test that no one meant to write.

## Server wiring and startup: how the process is assembled

Wiring tests check the objects `server.py` builds and shares, such as one campaign store shared by every campaign service it builds. Startup tests check that bad configuration ends the process with a clean message. Configuration is read at import, so these tests import the server in a fresh subprocess.

Good: the shared object is identified directly.

```python
service = fresh_server.campaign_service()
again = fresh_server.campaign_service()

assert service._store is again._store
```

Bad: testing startup config by importing `pro_behave.mcp.server` inside the test process. The module is already imported by `tests/conftest.py`, so the import does nothing and the test passes whatever the config.

## CLI: what a person sees

CLI tests run `pro_behave.cli.main(argv)` over real files, and check the exit code, stdout JSON or CSV, and the stderr message.

Good: the exit code, the message a person reads, and proof that nothing was written.

```python
error = run(["campaign", "create", "--release", "bogus"], expect=2)

assert "unknown release: bogus" in error
assert not campaign_file.exists()
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
