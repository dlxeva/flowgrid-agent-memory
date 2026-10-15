"""Opt-in source-bound calendar hints, never fact validity or authority.

The caller selects an exact isolated EvidenceSpan and supplies the source's
local utterance date. This module does not locate events, infer subjects, read
the clock, call models, or mutate memory. Dates are not UTC instants.
"""
from dataclasses import dataclass
from datetime import date, timedelta
import re

from .extraction import EvidenceSpan


class SourceCalendarError(ValueError):
    """Safe failure category; does not disclose source contents."""


@dataclass(frozen=True)
class CalendarWindow:
    start: str
    end_exclusive: str
    interpretation: str


@dataclass(frozen=True)
class SourceCalendarHint:
    evidence: EvidenceSpan
    anchor_date: str
    granularity: str
    windows: tuple[CalendarWindow, ...]
    ambiguous: bool
    status: str = "candidate"
    semantic_support: str = "unverified"
    version: str = "flowgrid.source-calendar/v1"


def resolve_source_calendar(source_text, evidence, *, anchor_date, enabled=False,
                            week_semantics=None):
    """Resolve only the selected literal phrase against a supplied local date.

    last week defaults to both prior ISO calendar week and preceding seven
    calendar days. A host may explicitly select calendar_week or rolling_days;
    that policy does not establish which meaning the speaker intended.
    """
    if enabled is not True:
        raise SourceCalendarError("source_calendar_disabled")
    if type(evidence) is not EvidenceSpan or not isinstance(source_text, str):
        raise SourceCalendarError("invalid_source_evidence")
    if (type(evidence.start) is not int or type(evidence.end) is not int
            or not 0 <= evidence.start < evidence.end <= len(source_text)
            or source_text[evidence.start:evidence.end] != evidence.quote):
        raise SourceCalendarError("source_span_mismatch")
    if not isinstance(anchor_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", anchor_date):
        raise SourceCalendarError("local_source_date_required")
    try:
        anchor = date.fromisoformat(anchor_date)
    except ValueError:
        raise SourceCalendarError("invalid_source_date") from None
    if week_semantics not in (None, "calendar_week", "rolling_days"):
        raise SourceCalendarError("invalid_week_semantics")
    phrase = evidence.quote.casefold()
    if phrase not in ("last year", "去年", "yesterday", "昨天", "last week", "上周"):
        raise SourceCalendarError("unsupported_calendar_phrase")
    if phrase not in ("last week", "上周") and week_semantics is not None:
        raise SourceCalendarError("inapplicable_week_semantics")
    try:
        if phrase in ("last year", "去年"):
            intervals = [(date(anchor.year - 1, 1, 1), date(anchor.year, 1, 1), "calendar_year")]
            granularity = "year"
        elif phrase in ("yesterday", "昨天"):
            intervals = [(anchor - timedelta(days=1), anchor, "preceding_calendar_day")]
            granularity = "day"
        else:
            monday = anchor - timedelta(days=anchor.weekday())
            intervals = []
            if week_semantics in (None, "calendar_week"):
                intervals.append((monday - timedelta(days=7), monday, "calendar_week"))
            if week_semantics in (None, "rolling_days"):
                intervals.append((anchor - timedelta(days=7), anchor, "rolling_days"))
            granularity = "week"
    except (ValueError, OverflowError):
        raise SourceCalendarError("calendar_range_unrepresentable") from None
    return SourceCalendarHint(evidence, anchor_date, granularity,
                              tuple(CalendarWindow(a.isoformat(), b.isoformat(), name)
                                    for a, b, name in intervals),
                              phrase in ("last week", "上周") and week_semantics is None)
