"""The job process: it runs a job's command and writes the job's result."""

import json
import signal

from pro_e2e.behave.framework import BehaveFramework
from pro_e2e.job_process import JobProcess
from pro_e2e.jobs.adapters import LocalJobResultStore
from pro_e2e.jobs.domain.results import ObservedInstance
from pro_e2e.jobs.messages import Instance, InstanceStatus, JobRequest, Outcome
from tests.conftest import FakeFramework

JOB_ID = "job"
ROW = Instance(id="features/a.feature:10", name="A -- @1.1 ubuntu release")
OTHER_ROW = Instance(
    id="features/a.feature:11", name="A -- @1.2 ubuntu release"
)


def queued(tmp_path, instances=(ROW,)):
    """A queued job's directory, with its request."""
    store = LocalJobResultStore(tmp_path)
    store.write_request(
        JOB_ID, JobRequest(job_id=JOB_ID, instances=list(instances))
    )
    store.write_targets(JOB_ID)
    return tmp_path / JOB_ID, store


def element(instance, status):
    feature, line = instance.id.rsplit(":", 1)
    return {
        "type": "scenario",
        "name": instance.name,
        "location": "{}:{}".format(feature, line),
        "status": status,
        "steps": [],
    }


def report_command(job_dir, elements, returncode=0):
    """A command that writes ``elements`` as behave's report, then exits."""
    report = json.dumps([{"name": "A", "elements": elements}])
    path = job_dir / "report.json"
    return [
        "sh",
        "-c",
        "printf '%s' \"$0\" > {}; exit {}".format(path, returncode),
        report,
    ]


def test_a_job_decides_its_unit_s_result_from_its_report(tmp_path):
    job_dir, store = queued(tmp_path)
    process = JobProcess(job_dir, BehaveFramework())

    # behave also reports the other combo's row, skipped.
    process.run(
        report_command(
            job_dir, [element(ROW, "passed"), element(OTHER_ROW, "skipped")]
        )
    )

    result = store.read_result(JOB_ID)
    assert (result.outcome, result.returncode) == (Outcome.PASSED, 0)
    assert [i.id for i in result.instances] == [ROW.id]


def test_a_failure_to_read_the_result_is_an_error_result(tmp_path):
    job_dir, store = queued(tmp_path)
    process = JobProcess(job_dir, FakeFramework(error=ValueError("bad")))

    process.run(["sh", "-c", "exit 3"])

    result = store.read_result(JOB_ID)
    assert (result.outcome, result.returncode) == (Outcome.ERROR, 3)
    assert "ValueError('bad')" in result.reason


def test_a_kill_after_the_command_exits_changes_nothing(tmp_path):
    job_dir, store = queued(tmp_path)
    observed = [ObservedInstance(ROW.id, ROW.name, InstanceStatus.PASSED)]
    framework = FakeFramework(observed=observed)
    process = JobProcess(job_dir, framework)
    # The kill lands while the result is being decided.
    framework.on_observe = lambda: process.kill(signal.SIGTERM, None)

    process.run(["true"])

    result = store.read_result(JOB_ID)
    assert (result.killed, result.outcome) == (False, Outcome.PASSED)


def test_a_job_killed_before_it_starts_has_no_outcome(tmp_path):
    job_dir, store = queued(tmp_path)
    process = JobProcess(job_dir, FakeFramework())
    process.killed = True

    process.run(["true"])

    result = store.read_result(JOB_ID)
    assert (result.killed, result.outcome) == (True, None)
    assert result.reason == "killed before it started"


def test_a_command_that_cannot_start_is_an_error_result(tmp_path):
    job_dir, store = queued(tmp_path)
    process = JobProcess(job_dir, FakeFramework())

    process.run([str(tmp_path / "no-such-command")])

    result = store.read_result(JOB_ID)
    assert (result.outcome, result.returncode) == (Outcome.ERROR, None)
    assert "No such file" in result.reason
