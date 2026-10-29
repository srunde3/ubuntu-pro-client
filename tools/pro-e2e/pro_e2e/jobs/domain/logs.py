"""Reading a job's stdout log: selecting lines, and what a digest of its
failures holds."""

import re
from typing import NamedTuple, Sequence

from pro_e2e.jobs.domain.limits import DEFAULT_LOG_CONTEXT, DEFAULT_LOG_LINES


class LogSelection(NamedTuple):
    """Part of a log, as :func:`select_log_lines` returns it.

    ``first_line``/``last_line`` bound what was returned and are None when
    nothing was. ``matches`` counts every match from ``start`` on, whether
    or not it fit; ``truncated`` says some of the log asked for did not.
    """

    text: str
    first_line: int | None
    last_line: int | None
    matches: int
    truncated: bool


def _numbered(lines: Sequence[str], first: int, last: int) -> list[str]:
    """Lines ``first``..``last``, 1-based and inclusive, prefixed ``N: ``."""
    return [
        "{}: {}".format(number, lines[number - 1])
        for number in range(first, last + 1)
    ]


def select_log_lines(
    lines: Sequence[str],
    *,
    pattern: str = "",
    context: int = DEFAULT_LOG_CONTEXT,
    start: int = 0,
    limit: int = DEFAULT_LOG_LINES,
) -> LogSelection:
    """Pick the part of a log a caller asked for.

    With ``pattern`` (a regex, case-insensitive): every line matching it
    from ``start`` on, each with ``context`` lines either side, as grep
    would print them -- overlapping windows merge, and ``--`` separates
    the rest. Windows are taken in order until the next would push the
    output past ``limit`` lines.

    Without: ``limit`` lines from ``start``, or the last ``limit`` lines
    when ``start`` is 0.
    """
    total = len(lines)
    if not pattern:
        if start > 0:
            first = start
            last = min(total, start + limit - 1)
        else:
            first = max(1, total - limit + 1)
            last = total
        if total == 0 or first > total:
            return LogSelection("", None, None, 0, False)
        return LogSelection(
            "\n".join(_numbered(lines, first, last)),
            first,
            last,
            0,
            (last < total) if start > 0 else (first > 1),
        )

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as error:
        raise ValueError("invalid pattern {!r}: {}".format(pattern, error))

    hits = [
        number
        for number in range(max(start, 1), total + 1)
        if regex.search(lines[number - 1])
    ]
    windows: list[tuple[int, int]] = []
    budget = limit
    truncated = False
    for number in hits:
        low = max(1, number - context)
        high = min(total, number + context)
        if windows and low <= windows[-1][1] + 1:
            merged_high = max(windows[-1][1], high)
            cost = merged_high - windows[-1][1]
        else:
            cost = high - low + 1
        if cost > budget:
            truncated = True
            break
        budget -= cost
        if windows and low <= windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], max(windows[-1][1], high))
        else:
            windows.append((low, high))
    if not windows:
        return LogSelection("", None, None, len(hits), truncated)
    text = "\n--\n".join(
        "\n".join(_numbered(lines, low, high)) for low, high in windows
    )
    return LogSelection(
        text, windows[0][0], windows[-1][1], len(hits), truncated
    )


class StepRef(NamedTuple):
    line: int
    text: str


class LogRegion(NamedTuple):
    """One failure as the log shows it: a traceback, hook error or assert.

    ``exception`` is the line that names what went wrong -- for a chained
    traceback, the last one raised. ``text`` is the region, capped.
    """

    kind: str
    first_line: int
    last_line: int
    step: StepRef | None
    exception: str
    text: str


class LogSummary(NamedTuple):
    first_line: int
    last_line: int
    text: str


class LogDigest(NamedTuple):
    """What a failed job's log says, in order, without reading it."""

    total_lines: int
    finished: bool
    errors: list[LogRegion]
    errors_total: int
    summary: LogSummary | None
    tail: str
