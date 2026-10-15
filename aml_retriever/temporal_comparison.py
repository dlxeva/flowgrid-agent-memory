"""Opt-in, source-bound temporal arithmetic, not event/claim interpretation.

The host supplies authorized RawEvents and complete expression spans. Results
are conditional calculations: the host must still establish which event the
expression describes, negation, hypotheticals and source authority. No storage,
network, automatic extraction, confirmation or wall-clock fallback is used.
"""
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import re

from .date_expression import DateExpressionError, _anchor, _offset, resolve_date_expression
from .extraction import EvidenceSpan
from .governance import RawEvent

VERSION = 'flowgrid.temporal-comparison/v1'
WORDS = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4,
         'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
         'ten': 10, 'eleven': 11, 'twelve': 12,
         '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
         '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
APPROX = re.compile(r'^(?:about|around|approximately|roughly)\s+|^(?:大约|约)\s*', re.I)
EN = re.compile(r'([0-9]{1,4}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve) (days?|weeks?|months?) ago', re.I)
ZH = re.compile(r'([0-9]{1,4}|[一二两三四五六七八九十])(天|周|个?月)前')


class TemporalComparisonError(ValueError):
    """Safe failure codes; no private source text in messages."""


@dataclass(frozen=True)
class ApproximationPolicy:
    policy_id: str
    radius_days: int


@dataclass(frozen=True)
class TimeInterval:
    source_event_id: str
    user_id: str
    scope: tuple
    source_sha256: str
    source_anchor: str
    span_start: int
    span_end: int
    expression: str
    utc_offset: str | None
    earliest: str | None
    latest: str | None
    reason: str | None
    approximate: bool
    month_policy: str | None
    approximation_policy: ApproximationPolicy | None
    version: str = VERSION

    def to_dict(self):
        return asdict(self)


def _require(condition, code):
    if not condition:
        raise TemporalComparisonError(code)


def _source(source, span):
    _require(type(source) is RawEvent and type(span) is EvidenceSpan, 'typed_source_and_span_required')
    _require(isinstance(source.content, str) and 0 < len(source.content) <= 65536, 'invalid_source_content')
    _require(isinstance(source.id, str) and bool(source.id) and isinstance(source.user_id, str) and bool(source.user_id), 'invalid_source_identity')
    _require(isinstance(source.scope, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in source.scope.items()), 'invalid_source_scope')
    _require(span.source_event_id == source.id and type(span.start) is int and type(span.end) is int
             and 0 <= span.start < span.end <= len(source.content)
             and source.content[span.start:span.end] == span.quote, 'source_span_mismatch')
    _require(isinstance(span.quote, str) and 0 < len(span.quote) <= 256, 'invalid_time_expression')
    # Do not silently strip an adjacent approximation qualifier from the proof.
    preceding = source.content[max(0, span.start-30):span.start]
    _require(not re.search(r'(?:\babout|\baround|\bapproximately|\broughly|大约|约)\s*$', preceding, re.I), 'incomplete_approximation_span')


def resolve_time_quote(source, span, *, utc_offset=None, month_policy=None, approximation_policy=None):
    """Resolve a host-selected span; unknown for missing semantic time policy."""
    _source(source, span)
    _require(month_policy in (None, 'calendar_clamp', 'fixed_30_days'), 'invalid_month_policy')
    if approximation_policy is not None:
        p = approximation_policy
        _require(type(p) is ApproximationPolicy and isinstance(p.policy_id, str) and bool(p.policy_id.strip())
                 and len(p.policy_id) <= 128 and type(p.radius_days) is int
                 and 1 <= p.radius_days <= 365, 'invalid_approximation_policy')
    expression = span.quote
    approximate = APPROX.match(expression) is not None
    phrase = APPROX.sub('', expression, count=1)
    fields = dict(source_event_id=source.id, user_id=source.user_id, scope=tuple(sorted(source.scope.items())),
                  source_sha256=hashlib.sha256(source.content.encode()).hexdigest(), source_anchor=source.observed_at,
                  span_start=span.start, span_end=span.end, expression=expression, utc_offset=utc_offset,
                  approximate=approximate, month_policy=month_policy, approximation_policy=approximation_policy)

    def unknown(reason):
        return TimeInterval(**fields, earliest=None, latest=None, reason=reason)

    if approximate and approximation_policy is None:
        return unknown('approximation_range_required')
    match = EN.fullmatch(phrase) or ZH.fullmatch(phrase)
    try:
        if match:
            number, unit = match.groups()
            number = int(number) if number.isascii() and number.isdigit() else WORDS[number.lower()]
            if not 1 <= number <= 1200:
                return unknown('relative_count_out_of_range')
            zone = _offset(utc_offset)
            day = _anchor(source.observed_at).astimezone(zone).date()
            if unit.lower().startswith('day') or unit == '天':
                day -= timedelta(days=number)
            elif unit.lower().startswith('week') or unit == '周':
                day -= timedelta(days=7*number)
            else:
                if month_policy is None:
                    return unknown('month_semantics_required')
                if month_policy == 'fixed_30_days':
                    day -= timedelta(days=30*number)
                else:
                    year, month0 = divmod(day.year*12 + day.month-1-number, 12)
                    day = day.replace(year=year, month=month0+1,
                                      day=min(day.day, monthrange(year, month0+1)[1]))
            start = datetime.combine(day, time.min, zone)
            end = start + timedelta(days=1) - timedelta(microseconds=1)
        else:
            result = resolve_date_expression(phrase, anchor=source.observed_at, utc_offset=utc_offset)
            start = _anchor(result.value)
            width = {'day': timedelta(days=1), 'minute': timedelta(minutes=1),
                     'second': timedelta(seconds=1)}.get(result.granularity)
            end = start + width - timedelta(microseconds=1) if width else start
        if approximate:
            radius = timedelta(days=approximation_policy.radius_days)
            start, end = start-radius, end+radius
        return TimeInterval(**fields, earliest=start.astimezone(timezone.utc).isoformat(),
                            latest=end.astimezone(timezone.utc).isoformat(), reason=None)
    except (DateExpressionError, ValueError, OverflowError):
        return unknown('unsupported_expression_or_anchor')


def compare_time_quotes(left_source, left_span, right_source, right_span, *, left_options=None, right_options=None):
    """Compute only interval ordering under explicit host interpretation.

    No precomputed interval can bypass re-resolution of the supplied sources.
    Strict scope equality is necessary but not an access-control substitute.
    """
    _source(left_source, left_span); _source(right_source, right_span)
    _require(left_source.user_id == right_source.user_id and left_source.scope == right_source.scope,
             'cross_scope_comparison_rejected')
    left = resolve_time_quote(left_source, left_span, **(left_options or {}))
    right = resolve_time_quote(right_source, right_span, **(right_options or {}))
    relation, reason = 'unknown', 'intervals_overlap'
    if left.reason or right.reason:
        reason = 'unresolved_time_expression'
    elif _anchor(left.latest) < _anchor(right.earliest):
        relation, reason = 'before', None
    elif _anchor(left.earliest) > _anchor(right.latest):
        relation, reason = 'after', None
    return {'version': VERSION, 'relation': relation, 'reason': reason,
            'status': 'unknown' if relation == 'unknown' else 'conditional_calculation',
            'semantic_binding_verified': False, 'state_actions': 0,
            'left': left.to_dict(), 'right': right.to_dict()}
