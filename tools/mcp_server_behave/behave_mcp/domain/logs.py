"""Reading a job's stdout log: selecting lines and digesting errors."""

import re
from typing import NamedTuple, Sequence

from behave_mcp.domain.limits import DEFAULT_LOG_CONTEXT, DEFAULT_LOG_LINES


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


# A digest is bounded by construction: one hook error in the wild was 117
# lines of `lxc info` JSON, and a run can fail the same way many times.
DIGEST_HEAD_LINES = 25


DIGEST_TAIL_LINES = 8


DIGEST_MAX_LINE_CHARS = 400


DIGEST_MAX_REGIONS = 10


DIGEST_TAIL = 5


_STEP_RESULT = re.compile(
    r"^\s+(?:Given|When|Then|And|But) .* \.\.\. "
    r"(?:failed|error|hook_error|undefined) in [\d.]+s$"
)


_SCENARIO_START = re.compile(r"^\s+Scenario(?: Outline)?: ")


_TRACEBACK_START = "Traceback (most recent call last):"


_CHAINED = ("The above exception", "During handling of the above exception")


_HOOK_ERROR = re.compile(r"^HOOK-ERROR in \w+: ")


_ASSERT_FAILED = "ASSERT FAILED"


_SUMMARY_LIST = re.compile(r"^(?:Failing|Errored) scenarios:")


_SUMMARY_COUNTS = re.compile(r"^\d+ features? passed")


_SUMMARY_END = re.compile(r"^Took ")


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


def _clip(line: str) -> str:
    if len(line) <= DIGEST_MAX_LINE_CHARS:
        return line
    return line[:DIGEST_MAX_LINE_CHARS] + " …"


def _capped(lines: Sequence[str]) -> str:
    clipped = [_clip(line) for line in lines]
    if len(clipped) <= DIGEST_HEAD_LINES + DIGEST_TAIL_LINES:
        return "\n".join(clipped)
    omitted = len(clipped) - DIGEST_HEAD_LINES - DIGEST_TAIL_LINES
    return "\n".join(
        [
            *clipped[:DIGEST_HEAD_LINES],
            "... [{} lines omitted] ...".format(omitted),
            *clipped[-DIGEST_TAIL_LINES:],
        ]
    )


def _to_blank(lines: Sequence[str], index: int) -> int:
    """Index just past the block of non-blank lines starting at ``index``."""
    while index < len(lines) and lines[index].strip():
        index += 1
    return index


def _skip_blank(lines: Sequence[str], index: int) -> int:
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def _traceback_end(lines: Sequence[str], index: int) -> int:
    """Index just past a traceback, following chained-exception blocks.

    Python prints a chain as: exception, blank, "The above exception ...",
    blank, the next traceback. Each link is taken as part of the same
    region, since it is one failure.
    """
    end = _to_blank(lines, index)
    while True:
        marker = _skip_blank(lines, end)
        if marker >= len(lines) or not lines[marker].startswith(_CHAINED):
            return end
        following = _skip_blank(lines, marker + 1)
        if following >= len(lines) or not lines[following].startswith(
            _TRACEBACK_START
        ):
            return end
        end = _to_blank(lines, following)


def _raised(lines: Sequence[str]) -> str:
    """The line naming the exception: the first unindented line after the
    last stack frame. A multi-line message keeps only its first line."""
    last_frame = -1
    for position, line in enumerate(lines):
        if line.startswith('  File "'):
            last_frame = position
    for line in lines[last_frame + 1 :]:
        if line and not line[0].isspace():
            return _clip(line)
    return _clip(lines[-1]) if lines else ""


def digest_log(lines: Sequence[str]) -> LogDigest:
    """Pull the failures out of a behave log, in the order they happened.

    Regions are tied to the nearest failing step line above them, until a
    new scenario starts. The behave summary block and the last few lines
    come along so a run that never reached behave is still diagnosable.
    """
    total = len(lines)
    regions: list[LogRegion] = []
    summary: LogSummary | None = None
    step: StepRef | None = None
    index = 0
    while index < total:
        line = lines[index]
        if _SCENARIO_START.match(line):
            step = None
        elif _STEP_RESULT.match(line):
            step = StepRef(index + 1, line.strip())
        elif line.startswith(_TRACEBACK_START):
            end = _traceback_end(lines, index)
            block = lines[index:end]
            regions.append(
                LogRegion(
                    "traceback",
                    index + 1,
                    end,
                    step,
                    _raised(block),
                    _capped(block),
                )
            )
            index = end
            continue
        elif _HOOK_ERROR.match(line):
            end = _to_blank(lines, index)
            regions.append(
                LogRegion(
                    "hook_error",
                    index + 1,
                    end,
                    step,
                    _clip(line),
                    _capped(lines[index:end]),
                )
            )
            index = end
            continue
        elif line.startswith(_ASSERT_FAILED):
            end = index + 1
            while (
                end < total
                and lines[end].strip()
                and not lines[end].startswith(_TRACEBACK_START)
            ):
                end += 1
            exception = _clip(line)
            # behave prints the assertion, a blank, then its traceback:
            # the same failure, so one region.
            following = _skip_blank(lines, end)
            if following < total and lines[following].startswith(
                _TRACEBACK_START
            ):
                end = _traceback_end(lines, following)
                exception = _raised(lines[index:end])
            regions.append(
                LogRegion(
                    "assert",
                    index + 1,
                    end,
                    step,
                    exception,
                    _capped(lines[index:end]),
                )
            )
            index = end
            continue
        elif summary is None and (
            _SUMMARY_LIST.match(line) or _SUMMARY_COUNTS.match(line)
        ):
            end = index
            while end < total and not _SUMMARY_END.match(lines[end]):
                end += 1
            if end < total:
                summary = LogSummary(
                    index + 1, end + 1, "\n".join(lines[index : end + 1])
                )
                index = end + 1
                continue
        index += 1

    return LogDigest(
        total_lines=total,
        finished=summary is not None,
        errors=regions[:DIGEST_MAX_REGIONS],
        errors_total=len(regions),
        summary=summary,
        tail="\n".join(_clip(line) for line in lines[-DIGEST_TAIL:]),
    )
