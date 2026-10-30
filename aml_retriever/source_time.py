"""Render original message timestamps without substituting ingestion time.

Unix milliseconds identify an instant, not an event time or speaker timezone.
Unrepresentable values remain unknown. No clock access or inferred dates.
"""
from datetime import datetime,timedelta,timezone

_EPOCH=datetime(1970,1,1,tzinfo=timezone.utc)


def source_message_time(timestamp_ms):
    if type(timestamp_ms) is not int:return 'unknown'
    try:
        return (_EPOCH+timedelta(milliseconds=timestamp_ms)).isoformat()
    except (OverflowError,ValueError):
        return 'unknown'
