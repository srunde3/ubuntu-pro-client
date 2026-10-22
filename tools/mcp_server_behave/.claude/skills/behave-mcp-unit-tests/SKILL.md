---
name: behave-mcp-unit-tests
description: What unit tests in tools/mcp_server_behave test, layer by layer, with good and bad examples. Use when writing or reviewing tests for domain, service, reconcile, adapter or config code in behave_mcp or behave_campaign.
---

# behave MCP unit tests

A unit test proves one behaviour of one layer. It replaces everything past the layer's ports with fakes or injected lambdas, so it is fast, deterministic and says exactly what broke. It does not speak the MCP protocol, read the real `features/` corpus, or start tox or behave.

Each layer has a different job, so each is tested for a different thing. Two rules apply to every layer: tests are arranged, acted and asserted in that order, and every value in a test has a name that says what it stands for.

## Arrange, act, assert

A test has three parts, separated by blank lines: it arranges the inputs, acts once on the subject, and asserts on the result. A reader finds the behaviour under test in the act, and the expectation in the assertions below it.

Good: one act, and its result is named before it is checked.

```python
def test_the_actionable_preset(kind, actionable):
    patterns = expand_event_kinds([ACTIONABLE_PRESET])

    matched = event_matches(kind, patterns)

    assert matched is actionable
```

Bad: acts and asserts interleaved. The test checks four behaviours, and the first failure hides the rest.

```python
def test_run_and_pause(campaigns):
    campaigns.run(campaign_id=campaign_id)
    assert state(store) == CampaignState.RUNNING
    campaigns.pause(campaign_id=campaign_id)
    assert state(store) == CampaignState.PAUSED
    campaigns.run(campaign_id=campaign_id)
    assert state(store) == CampaignState.RUNNING
```

Write one test per behaviour, or a table when the behaviours are cases of one rule. When the behaviour is itself a sequence, such as what repeated reconciles do, the earlier steps are part of the arrangement. Name the test after the sequence: `test_starvation_is_reported_once_per_episode`.

## Name every value

A value that matters to the test gets a name that says why it is there. A value that does not matter gets a name that says so. A reader then never has to guess whether `2` is a count, an id or a coincidence.

Good: the name says the exact value is irrelevant, and the id comes from the arrangement.

```python
# Any count: the test is about the limit, not the number.
SHOWN = 2

result = service.job_results(limit=SHOWN)
```

```python
campaign_id = create(store)
store.append(campaign_id, [IntentRecord(Intent.HOLD, Cause.PAUSE, AT)])
```

Good: two ids whose names say which is which.

```python
JOB_ID = "job-under-test"
OTHER_JOB_ID = "job-from-another-run"
```

Bad: bare literals. Is `2` special? Which of `"1"` and `"2"` is the job under test?

```python
service.job_results(limit=2)
service.job_results(job_ids=["1"])
assert [r.job_id for r in results] == ["1", "2"]
```

## Domain: the rules

Domain tests check a rule as a function of data: how records reduce to unit states, what a reconcile decides for a campaign, how a result is classified, what the log digest finds. Build the input, call the function, assert on the decision.

Good: the rule and its input are visible, and nothing else runs.

```python
def test_a_busy_wsl_job_does_not_hold_back_lxd(self):
    records = [
        *plan(UNIT_W1, UNIT_W2, UNIT_A, UNIT_B),
        IntentRecord(Intent.RUN, Cause.RUN, AT),
        attempt(UNIT_W1, "running"),
    ]
    jobs = [InFlightJob(unit=UNIT_W1, job_id="job1")]

    decided = plan_campaign(
        records=records, jobs=jobs, max_jobs={Pool.LXD: 2, Pool.WSL: 1}, at=AT
    )

    assert set(decided.queue) == {UNIT_A, UNIT_B}
```

Bad: the same rule tested through the service and a store. A failure could be the store, the service or the rule, and the test is ten times longer.

```python
def test_wsl_does_not_block_lxd(campaigns, store, jobs):
    campaign_id = create(store, units=[*WSL_UNITS, *UNITS], max_jobs={Pool.LXD: 2, Pool.WSL: 1})
    campaigns.run(campaign_id=campaign_id)
    campaigns.reconcile()
    assert len(jobs.queued) == 3
```

## Services: the orchestration

Service tests check what the service does with its ports: which port it calls and with what, what it persists, and which caller mistakes it refuses before touching anything.

Good: a refusal is proven by the port never being called.

```python
with pytest.raises(BehaveServiceError, match="exact name"):
    service.run_job(
        "features/cli/attach.feature",
        scenario="Attach command",
        release="jammy",
        machine_type="lxd-container",
    )
assert launcher.calls == []
```

Bad: patching the service's own private method. The test then proves the patch, and breaks on any refactor.

```python
monkeypatch.setattr(service, "_state", lambda *_: JobState.QUEUED)
```

Time enters `BehaveService` through `monotonic`, `sleep`, `now_utc` and `new_job_id`, and `CampaignService` through `now` and `monotonic`. A test of waiting or timestamps passes lambdas for them. It never sleeps.

## Reconcile: one step at a time

Nothing runs on its own, so a test of what reconciles do calls `reconcile` itself, where a tool call, a CLI command or a finished job would, and finishes fake jobs with `FakeJobRunner` when it chooses. A restart is a new service over the same store.

Good: each step is an explicit reconcile, so the order of events is certain.

```python
campaigns.reconcile()
campaigns.reconcile()
assert len(starved()) == 1

jobs.full_pools.clear()
campaigns.reconcile()
```

Bad: a background thread and a sleep. The test is slow, and it fails when the machine is busy.

```python
threading.Thread(target=server._reconcile_forever, daemon=True).start()
campaigns.run(campaign_id=campaign_id)
time.sleep(1)
assert len(jobs.queued) == 2
```

## Adapters: the real effect

Adapter tests check the effect an adapter has on the outside world: the file it writes, the process it starts, the lock it takes. Use the real filesystem under `tmp_path` and small real processes.

Good: the behaviour itself is observed.

```python
_launch(tmp_path, lanes, launcher, "job", ["sh", "-c", "read line; echo got=$line"])

assert _eventually(lambda: _exit(tmp_path, "job") is not None)
assert log.read_text().strip() == "got="
```

Good: a crash shown with a real crash. The job process is killed outright, and the test observes what is left: no exit, a free lock and lane, and no orphaned child.

```python
os.kill(job_process, signal.SIGKILL)

assert _eventually(lambda: not launcher.is_running("job"))
assert _exit(tmp_path, "job") is None
assert _eventually(lambda: not _pid_alive(child))
```

Weak on its own: asserting the arguments passed to a patched `Popen`. It proves the call, not that the child really reads no stdin. Keep such a test only alongside one that observes the effect.

```python
assert calls["stdin"] == subprocess.DEVNULL
```

## On-disk formats

Golden tests pin the exact bytes of job records and exits. A failure means the format changed. Update the expected bytes only when the change is deliberate.

## Fakes, not mocks

A fake is a small working implementation of a port. It behaves like the real thing in the ways the test needs, records what it was asked, and fails loudly on anything else. A mock accepts every call and returns another mock, so a test built on one passes even when the code calls the wrong method or passes the wrong shape.

Good: a fake with the port's real signature, which records calls and behaves like the real thing where the service can tell: a job runs until the test ends it, and ending it writes the exit the way the job process does.

```python
class FakeJobLauncher:
    def launch(self, job_id, command, cwd, env, lease, record):
        self.calls.append({"job_id": job_id, "command": command, ...})
        record()  # the real launcher holds the job's lock by now
        lease.close()
        if self._error is not None:
            lease.release()
            raise self._error
        self.running.add(job_id)

    def is_running(self, job_id):
        return job_id in self.running

    def finish(self, job_id, returncode=0, killed=False, log="", report=None):
        ...  # writes the log, the report, then exit.json
```

Bad: a mock standing in for the port. A typo in `launch`, or a method the port never had, still passes.

```python
launcher = MagicMock()
launcher.is_running.return_value = True
service.run_job(
    "features/cli/attach.feature", "Attach command", "jammy", "lxd-container"
)
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

Good: several dimensions. The table is keyed by every combination, and the completeness test builds the full product. A combination that cannot arise is in the table too, marked `None`, so the test says it is refused rather than leaving it out. The test ids read like `hold-open-True`.

```python
NAMED_STATE = {
    (Intent.NEVER_RUN, Progress.UNTOUCHED, False): CampaignState.CREATED,
    (Intent.NEVER_RUN, Progress.OPEN, False): None,
    (Intent.RUN, Progress.OPEN, True): CampaignState.RUNNING,
    (Intent.HOLD, Progress.OPEN, True): CampaignState.PAUSING,
    # ... one row for every intent, progress and in-flight flag
}


def test_every_combination_has_an_expectation():
    assert set(NAMED_STATE) == set(
        itertools.product(Intent, Progress, (False, True))
    )


@pytest.mark.parametrize(
    "intent_, progress_, busy",
    [key for key, state in NAMED_STATE.items() if state is not None],
    ids=str,
)
def test_campaign_state_names_a_reachable_combination(intent_, progress_, busy):
    expected = NAMED_STATE[(intent_, progress_, busy)]

    assert campaign_state(intent_, progress_, busy) == expected
```

Good: a decision function whose result is an enum. Name each case with `pytest.param(..., id=...)`, and check that the cases reach every result.

```python
UNIT_MATCH_CASES = [
    pytest.param(
        [(SCENARIO, {JAMMY_LXD})], UnitMatch.FOUND, id="one-scenario"
    ),
    pytest.param(
        [(SCENARIO, {NOBLE_VM})],
        UnitMatch.NOT_ON_COMBO,
        id="not-on-this-combo",
    ),
    # ... one case for every result
]


def test_every_unit_match_is_reached_by_a_case():
    assert {case.values[1] for case in UNIT_MATCH_CASES} == set(UnitMatch)


@pytest.mark.parametrize("scenarios, expected", UNIT_MATCH_CASES)
def test_match_unit(scenarios, expected):
    release, machine_type = JAMMY_LXD

    match = match_unit(scenarios, SCENARIO, release, machine_type)

    assert match == expected
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

Run the unit tests with `uv run pytest -q -m "not integration and not e2e"`, or one file or test with `uv run pytest -q tests/behave_campaign/test_service_commands.py -k starvation`.
