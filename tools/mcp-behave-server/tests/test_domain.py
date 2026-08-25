"""Plain unit tests for pure domain logic."""

from behave_mcp import domain


def test_classify_job_status_live_handle_running():
    result = domain.classify_job_status(
        has_live_handle=True,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=True,
    )
    assert result.status == "running"
    assert result.ok is None
    assert result.reason == "live_handle_running"


def test_classify_job_status_live_handle_exited_ok():
    result = domain.classify_job_status(
        has_live_handle=True,
        returncode=0,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "completed"
    assert result.ok is True
    assert result.reason == "live_handle_exited"


def test_classify_job_status_live_handle_exited_failed():
    result = domain.classify_job_status(
        has_live_handle=True,
        returncode=1,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "completed"
    assert result.ok is False
    assert result.reason == "live_handle_exited"


def test_classify_job_status_recovered_report_present_ok():
    result = domain.classify_job_status(
        has_live_handle=False,
        returncode=None,
        report_present=True,
        report_ok=True,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "completed"
    assert result.ok is True
    assert result.reason == "report_present"


def test_classify_job_status_recovered_report_present_failed():
    result = domain.classify_job_status(
        has_live_handle=False,
        returncode=None,
        report_present=True,
        report_ok=False,
        pid=123,
        pid_alive=True,
    )
    assert result.status == "completed"
    assert result.ok is False
    assert result.reason == "report_present"


def test_classify_job_status_recovered_pid_alive_no_report():
    result = domain.classify_job_status(
        has_live_handle=False,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=True,
    )
    assert result.status == "running"
    assert result.ok is None
    assert result.reason == "pid_alive_no_report"


def test_classify_job_status_recovered_pid_dead_no_report():
    result = domain.classify_job_status(
        has_live_handle=False,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "unknown"
    assert result.ok is False
    assert result.reason == "pid_dead_no_report"


def test_classify_job_status_recovered_pid_unknown_no_report():
    result = domain.classify_job_status(
        has_live_handle=False,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=None,
        pid_alive=False,
    )
    assert result.status == "unknown"
    assert result.ok is False
    assert result.reason == "pid_unknown_no_report"
