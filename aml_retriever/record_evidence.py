"""Pure evidence rendering with explicit message-record metadata.

Original quotes are copied only from validated spans. No inference, source
merging, deduplication, timestamp conversion, or model invocation occurs here.
"""
import copy
import json


def render_records(rows):
    """Render each original span independently and rebuild quote offsets.

    Every nonempty row must supply ordered, nonoverlapping source spans.
    Existing row/span fields are retained, except content and rendered offsets.
    """
    if not isinstance(rows, list):
        raise ValueError('record_rows')
    result = []
    for row in rows:
        if not isinstance(row, dict) or 'id' not in row or not isinstance(row.get('content'), str):
            raise ValueError('record_row')
        content = row['content']
        source_spans = row.get('source_spans')
        if not isinstance(source_spans, list) or not source_spans:
            raise ValueError('record_source_spans')
        parts, spans, offset, previous_end = [], [], 0, 0
        for span in source_spans:
            if not isinstance(span, dict):
                raise ValueError('record_source_span')
            start, end = span.get('start'), span.get('end')
            if type(start) is not int or type(end) is not int or not previous_end <= start < end <= len(content):
                raise ValueError('record_source_bounds')
            if any(not isinstance(span.get(k), str) or not span[k]
                   for k in ('source_id', 'role', 'message_time')):
                raise ValueError('record_source_metadata')
            previous_end = end
            metadata = {'source_id': span['source_id'],
                        'speaker_role': span['role'],
                        'message_recorded_at': span['message_time']}
            prefix = json.dumps(metadata, ensure_ascii=False, separators=(',', ':')) + '\nOriginal quote:\n'
            quote = content[start:end]
            part = prefix + quote
            rendered_span = copy.deepcopy(span)
            rendered_span['start'] = offset + len(prefix)
            rendered_span['end'] = rendered_span['start'] + len(quote)
            parts.append(part)
            spans.append(rendered_span)
            offset += len(part) + 1
        rendered = copy.deepcopy(row)
        rendered['content'] = '\n'.join(parts)
        rendered['source_spans'] = spans
        result.append(rendered)
    return result
