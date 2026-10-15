"""Explicit host date normalization, not sentence/validity interpretation.

Only isolated, fully matched expressions. Relative days need an evidence-time
anchor and an explicit fixed local offset. No wall clock or implicit timezone.
Day-granularity values are local midnight, not claims of an exact stated time.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import re

VERSION = 'flowgrid.date-expression/v1'
EXTENDED_VERSION = 'flowgrid.date-expression/v2'
RELATIVE = {'today':0,'tomorrow':1,'day after tomorrow':2,'今天':0,'明天':1,'后天':2}
EXTENDED_RELATIVE = {'the day after tomorrow':2}
DATE = re.compile(r'(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?')
ZH_DATE = re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?')
ZH_DATE_ADJACENT = re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{2}):(\d{2})(?::(\d{2}))?')
OFFSET = re.compile(r'([+-])(\d{2}):(\d{2})')
AWARE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})')

class DateExpressionError(ValueError):
    pass

@dataclass(frozen=True)
class DateResolution:
    expression: str
    anchor: str | None
    utc_offset: str | None
    value: str
    granularity: str
    version: str = VERSION

    def to_dict(self):
        return asdict(self)

def _offset(value):
    match = OFFSET.fullmatch(value) if isinstance(value,str) else None
    if match is None:
        raise DateExpressionError('explicit_fixed_offset_required')
    sign,hours,minutes=match.groups()
    hours,minutes=int(hours),int(minutes)
    if hours>14 or minutes>59 or (hours==14 and minutes):
        raise DateExpressionError('invalid_fixed_offset')
    return timezone(timedelta(minutes=(hours*60+minutes)*(1 if sign=='+' else -1)))

def _anchor(value):
    try:
        if not isinstance(value,str):
            raise ValueError()
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed
    except ValueError:
        raise DateExpressionError('aware_evidence_anchor_required') from None

def resolve_date_expression(expression, *, anchor=None, utc_offset=None):
    """Normalize a host-selected date phrase; grants no semantic authority."""
    if not isinstance(expression,str) or not expression or len(expression)>256 or expression!=expression.strip():
        raise DateExpressionError('unsupported_date_expression')
    if AWARE.fullmatch(expression):
        value=_anchor(expression).astimezone(timezone.utc).isoformat()
        # Self-contained aware instants need no external anchor/offset.
        if anchor is not None:
            _anchor(anchor)
        if utc_offset is not None:
            _offset(utc_offset)
        return DateResolution(expression,anchor,utc_offset,value,'instant')
    version=VERSION
    relative=expression.casefold() in RELATIVE
    match=DATE.fullmatch(expression) or ZH_DATE.fullmatch(expression)
    relative_days=RELATIVE.get(expression.casefold())
    if not relative and not match:
        relative_days=EXTENDED_RELATIVE.get(expression.casefold())
        relative=relative_days is not None
        match=ZH_DATE_ADJACENT.fullmatch(expression)
        if relative or match:
            version=EXTENDED_VERSION
    if not relative and not match:
        raise DateExpressionError('unsupported_date_expression')
    zone=_offset(utc_offset)
    try:
        if relative:
            day=_anchor(anchor).astimezone(zone).date()+timedelta(days=relative_days)
            parsed=datetime(day.year,day.month,day.day,tzinfo=zone)
            granularity='day'
        else:
            if anchor is not None:
                _anchor(anchor)
            year,month,day,hour,minute,second=match.groups()
            parsed=datetime(int(year),int(month),int(day),int(hour or 0),int(minute or 0),int(second or 0),tzinfo=zone)
            granularity='day' if hour is None else ('minute' if second is None else 'second')
    except (ValueError,OverflowError) as exc:
        if isinstance(exc,DateExpressionError):
            raise
        raise DateExpressionError('invalid_calendar_date') from None
    return DateResolution(expression,anchor,utc_offset,parsed.astimezone(timezone.utc).isoformat(),granularity,version)

def validate_resolution(resolution):
    if type(resolution) is not DateResolution:
        raise DateExpressionError('invalid_date_resolution')
    expected=resolve_date_expression(resolution.expression,anchor=resolution.anchor,utc_offset=resolution.utc_offset)
    if expected!=resolution:
        raise DateExpressionError('date_resolution_changed')
    return expected
