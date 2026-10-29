"""How much a response carries, and how long a wait lasts."""

DEFAULT_RUNNING_TAIL_LINES = 12


# 500 lines of a behave log is ~90 KB, about what one tool result may
# carry; the tail rarely holds the cause anyway (job_errors does).
DEFAULT_LOG_LINES = 100


MAX_LOG_LINES = 500


DEFAULT_LOG_CONTEXT = 3


MAX_LOG_CONTEXT = 20


DEFAULT_WAIT_TIMEOUT_SECONDS = 1800


DEFAULT_WAIT_POLL_INTERVAL_SECONDS = 5.0


DEFAULT_JOB_LIST_LIMIT = 20


MAX_JOB_LIST_LIMIT = 500


DEFAULT_RESULTS_LIMIT = 20


MAX_RESULTS_LIMIT = 500


# An unfiltered search hits every scenario in the suite (hundreds).
DEFAULT_FIND_LIMIT = 50


MAX_FIND_LIMIT = 500


# A digest is bounded by construction: one hook error in the wild was 117
# lines of `lxc info` JSON, and a run can fail the same way many times.
DIGEST_HEAD_LINES = 25


DIGEST_TAIL_LINES = 8


DIGEST_MAX_LINE_CHARS = 400


DIGEST_MAX_REGIONS = 10


DIGEST_TAIL = 5
