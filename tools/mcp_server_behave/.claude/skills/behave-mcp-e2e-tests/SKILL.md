---
name: behave-mcp-e2e-tests
description: What the end-to-end tests in tools/mcp_server_behave test, with good and bad examples. They start real tox and behave subprocesses. Use when writing or reviewing an e2e test, or when a change to the scheduler, the lane runner, job launch, polling, killing or job artifacts needs proof with real jobs.
---

# behave MCP end-to-end tests

An e2e test proves what only real jobs can show. Several behave subprocesses really run at once. A real report lands where the server reads it. A real result is classified the way the fakes assume. Everything else is cheaper to prove in a unit or integration test, so an e2e test asserts on those few things and nothing more.

Each e2e test carries `@pytest.mark.e2e` and `@pytest.mark.long_running`, so the fast suite (`-m "not e2e"`) skips it.

| Test | Needs | Proves |
| --- | --- | --- |
| `tests/mcp_surface/test_campaign_e2e.py` | tox; no LXD, no token | lanes really fill concurrently, every unit is recorded, reports exist, the event stream is dense |
| `tests/mcp_surface/test_e2e.py` | LXD and `UACLIENT_BEHAVE_CONTRACT_TOKEN`; skips without the token | one real attach job through the MCP protocol: start, list, wait, logs, artifacts, results |

## What makes a good e2e test

Good: it asserts on what the fakes cannot show, and its message reports what was seen.

```python
assert peak_lanes >= LANES, (
    "only ever saw {} of {} lanes in flight at once; the sliding window "
    "is not filling to capacity".format(peak_lanes, LANES)
)
```

Bad: an e2e test for a rule. Classifying a skipped result is a unit test of `classify_result`. Here it costs a tox run and fails for reasons unrelated to the rule.

Good: it chooses work that needs no machine when the machine is not the point. `features/_version.feature`'s attached-version scenario skips without `contract_token` config, before anything is provisioned, so the campaign and job tests prove the scheduler, the job process and the locks on any host with tox.

Bad: it depends on a token or LXD without saying so. Check for the requirement and `pytest.skip` with a reason, as `test_e2e.py` does for the token.

Good: it owns its config and its state. It points `MCP_STATE_DIR` under `tmp_path`, so every job and campaign file is its own, and sets the config it needs there.

```python
monkeypatch.setenv("MCP_STATE_DIR", str(tmp_path / "state"))
service = create_service()
service.set_config("max_jobs.lxd", str(LANES))
```

Good: it proves what no caller does. Only the job process's own reconcile can start a queued job when the test makes nothing but reads, so two jobs on one lane finishing back to back proves it.

Good: it always cleans up. `runner.shutdown()` and `kill_job` on a still-running job sit in a `finally` block. A leaked behave process holds an LXD instance or a lane after the test ends.

Good: it proves what crosses a process boundary from another process. Job state and kills are read off locks and files, so a second interpreter, started with `subprocess`, stands in for a restarted server or the CLI.

Bad: it waits a fixed time and then asserts. Real jobs take as long as they take. Sample state against a deadline, and record what you need, such as the peak, while sampling.

## Run

From `tools/mcp_server_behave`:

```bash
uv run pytest -q -m "e2e and long_running" tests/mcp_surface/test_campaign_e2e.py
uv run pytest -q -m "e2e and long_running" tests/mcp_surface/test_e2e.py
```

The campaign test takes about 20 s once tox's behave environment exists. The LXD test can take up to 30 min.
