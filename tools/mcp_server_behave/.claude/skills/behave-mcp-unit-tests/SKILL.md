---
name: behave-mcp-unit-tests
description: What unit tests in tools/mcp_server_behave test, layer by layer, with good and bad examples. Use when writing or reviewing tests for domain, service, runner, adapter or config code in behave_mcp or behave_campaign.
---

# behave MCP unit tests

A unit test proves one behaviour of one layer. It replaces everything past the layer's ports with fakes or injected lambdas, so it is fast, deterministic and says exactly what broke. It does not speak the MCP protocol, read the real `features/` corpus, or start tox or behave.

Each layer has a different job, so each is tested for a different thing.

## Domain: the rules

Domain tests check a rule as a function of data: how records reduce to unit states, what a tick decides, how a result is classified, what the log digest finds. Build the input, call the function, assert on the decision.

Good: the rule and its input are visible, and nothing else runs.

```python
def test_a_busy_wsl_lane_does_not_hold_back_lxd(self):
    records = [
        *plan(UNIT_W1, UNIT_W2, UNIT_A, UNIT_B),
        LifecycleRecord(Lifecycle.RUNNING, AT),
        attempt(UNIT_W1, "running"),
    ]
    lanes = [Lane(unit=UNIT_W1, job_id="job1")]

    tick = plan_tick(
        records=records, lanes=lanes, lane_limits={Pool.LXD: 2, Pool.WSL: 1}, at=AT
    )

    assert set(tick.start) == {UNIT_A, UNIT_B}
```

Bad: the same rule tested through the runner and a store. A failure could be the store, the runner or the rule, and the test is ten times longer.

```python
def test_wsl_does_not_block_lxd(runner, store, lanes):
    create(store, units=[*WSL_UNITS, *UNITS], lanes={Pool.LXD: 2, Pool.WSL: 1})
    runner.start("1234567")
    runner.tick("1234567")
    assert len(lanes.started) == 3
```

## Services: the orchestration

Service tests check what the service does with its ports: which port it calls and with what, what it persists, and which caller mistakes it refuses before touching anything.

Good: a refusal is proven by the port never being called.

```python
with pytest.raises(BehaveServiceError, match="exact name"):
    service.start_scenario(
        "features/cli/attach.feature",
        machine_types=["lxd-container"],
        scenario_name="Attach command",
    )
assert launcher.calls == []
```

Bad: patching the service's own private method. The test then proves the patch, and breaks on any refactor.

```python
monkeypatch.setattr(service, "_scenario_names", lambda *_: set())
```

Time enters a service through `monotonic`, `sleep`, `now_utc` and `new_job_id`. A test of waiting or timestamps passes lambdas for them. It never sleeps.

## Runner: the tick loop

Runner tests check what a sequence of ticks does to a campaign's records and events. The test drives `runner.tick` itself, with a ticker that never runs on its own, and finishes fake jobs when it chooses.

Good: each step is an explicit tick, so the order of events is certain.

```python
runner.tick("1234567")
runner.tick("1234567")
assert len(starved()) == 1

lanes.full_pools.clear()
runner.tick("1234567")
```

Bad: a real thread and a sleep. The test is slow, and it fails when the machine is busy.

```python
runner = CampaignRunner(..., ticker=ThreadTicker(interval=0.1))
runner.start("1234567")
time.sleep(1)
assert len(lanes.started) == 2
```

Only the ticker's own test runs a real thread, and it waits on a condition against a deadline.

## Adapters: the real effect

Adapter tests check the effect an adapter has on the outside world: the file it writes, the process it starts, the lock it takes. Use the real filesystem under `tmp_path` and small real processes.

Good: the behaviour itself is observed.

```python
handle = PopenLauncher().launch(
    ["sh", "-c", "read line; echo got=$line"], str(tmp_path), env, log_path
)
assert _eventually(lambda: handle.poll() is not None)
assert log_path.read_text().strip() == "got="
```

Weak on its own: asserting the arguments passed to a patched `Popen`. It proves the call, not that the child really reads no stdin. Keep such a test only alongside one that observes the effect.

```python
assert calls["stdin"] == subprocess.DEVNULL
```

## On-disk formats

Golden tests pin the exact bytes of job records and index rows. A failure means the format changed. Update the expected bytes only when the change is deliberate.

## Fakes, not mocks

A fake is a small working implementation of a port. It behaves like the real thing in the ways the test needs, records what it was asked, and fails loudly on anything else. A mock accepts every call and returns another mock, so a test built on one passes even when the code calls the wrong method or passes the wrong shape.

Good: a fake with the port's real signature, which records calls and returns a real-looking handle.

```python
class FakeLauncher:
    def __init__(self, handle=None, error=None, alive_pids=None):
        self.calls = []
        self._handle = handle if handle is not None else FakeProcessHandle()
        self._error = error
        self._alive_pids = set(alive_pids) if alive_pids else set()

    def launch(self, command, cwd, env, stdout_log_path):
        self.calls.append({"command": command, "cwd": cwd, "env": env, ...})
        if self._error is not None:
            raise self._error
        return self._handle
```

Bad: a mock standing in for the port. A typo in `launch`, or a handle method the code never had, still passes.

```python
launcher = MagicMock()
launcher.launch.return_value.poll.return_value = None
service.start_scenario("features/cli/attach.feature", ["lxd-container"])
launcher.launch.assert_called_once()
```

A mock is acceptable only when the test cares that a call happened and nothing about what it does, for example a callback that must fire once.

## Show that coverage is complete

A reader must be able to see from the test file that every case of the subject is covered. When the subject is defined by an enum or a small set of inputs, write the expectation down as a table keyed by every case. Add a test that the table covers the whole domain, and parametrize the behaviour test over the table. When someone adds an enum member, the completeness test fails until the table says what the new member should do.

Good: one enum. The table is the specification, and `ids=str` names each case after its member.

```python
EVENT_FOR_OUTCOME = {
    Outcome.PASSED: EventKind.UNIT_PASSED,
    Outcome.FAILED: EventKind.UNIT_FAILED,
    Outcome.SKIPPED: EventKind.UNIT_SKIPPED,
    Outcome.ERROR: EventKind.UNIT_ERRORED,
}


def test_every_outcome_has_an_expected_event():
    assert set(EVENT_FOR_OUTCOME) == set(Outcome)


@pytest.mark.parametrize("outcome, kind", EVENT_FOR_OUTCOME.items(), ids=str)
def test_each_outcome_announces_its_own_event(outcome, kind):
    assert unit_event_kind(outcome) == kind
```

Good: two dimensions. The table is keyed by every combination, and the completeness test builds the full product. The test ids read like `failed-True-True`.

```python
SCHEDULABLE = {
    (UnitState.UNATTEMPTED, False): True,
    (UnitState.UNATTEMPTED, True): True,
    (UnitState.RUNNING, False): False,
    (UnitState.RUNNING, True): False,
    (UnitState.FAILED, False): False,
    (UnitState.FAILED, True): True,
    # ... one row for every state and flag
}


def test_every_state_and_retry_flag_has_an_expectation():
    assert set(SCHEDULABLE) == {
        (state, retry) for state in UnitState for retry in (False, True)
    }


@pytest.mark.parametrize(
    "state, retry_pending, expected",
    [(state, retry, want) for (state, retry), want in SCHEDULABLE.items()],
    ids=str,
)
def test_schedulable_by_state_and_retry(state, retry_pending, expected):
    status = UnitStatus(
        unit=UNIT, state=state, job_id=None, attempts=(), retry_pending=retry_pending
    )
    assert is_schedulable(status) is expected
```

Good: a decision function whose result is an enum. Name each case with `pytest.param(..., id=...)`, and check that the cases reach every result.

```python
CASES = [
    pytest.param(
        dict(has_live_handle=True, returncode=None),
        (RunStatus.RUNNING, None, ClassifyReason.LIVE_HANDLE_RUNNING),
        id="live-running",
    ),
    pytest.param(
        dict(pid=1, pid_alive=False),
        (RunStatus.UNKNOWN, False, ClassifyReason.PID_DEAD_NO_REPORT),
        id="pid-dead",
    ),
    # ... one case for every reason
]


def test_every_reason_is_reached_by_a_case():
    assert {case.values[1][2] for case in CASES} == set(ClassifyReason)


@pytest.mark.parametrize("signals, expected", CASES)
def test_classify_job_state(signals, expected):
    assert tuple(classify_job_state(**{**DEFAULTS, **signals})) == expected
```

Weak: a hand-written list of cases. It may cover every member today, but nothing fails when a member is added.

```python
@pytest.mark.parametrize(
    "state,kind",
    [("passed", "unit.passed"), ("failed", "unit.failed"), ...],
)
```

Bad: a loop inside one test. The first failing case hides the rest, and the report names no case.

```python
def test_every_outcome():
    for outcome in Outcome:
        assert unit_event_kind(outcome).startswith("unit.")
```

## Signs of a good unit test

- Its name states the behaviour: `test_starvation_is_reported_once_per_episode`.
- It fails for one reason, and the failure says which.
- It asserts on what the layer returns or writes, not on how it got there.
- It uses the fakes in `tests/conftest.py`. When a test needs a fake to do more, it extends that fake, so each port keeps exactly one.
- When a fix changes behaviour that a test pinned, the test changes to the correct behaviour, and its name changes if the name described the old one.

Run the unit and integration tests together with `uv run pytest -q -m "not e2e"`.
