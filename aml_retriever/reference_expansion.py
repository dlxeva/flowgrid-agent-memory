"""Experimental bounded reference expansion over tenant-owned originals.

Opt-in only. Reference equality does not establish subject identity or privacy;
callers must not treat this as a relevance or disclosure filter. No model calls.
"""
import copy
import json
import re
from .source_time import source_message_time

# Chinese prose commonly touches Latin ticket IDs without whitespace.  Only
# ASCII identifier characters make an ID embedded in a larger identifier.
REFERENCE = re.compile(r'(?<![A-Za-z0-9_])(?:[A-Z]{2,8}[- ]?\d{3,12})(?![A-Za-z0-9_])')

# Limits apply only to overflow attachments; ordinary spare-row appends retain
# their previous behavior. Whole original messages are never truncated.
MAX_OVERFLOW_SOURCES = 16
MAX_OVERFLOW_UTF8_BYTES = 16 * 1024
MAX_OVERFLOW_PER_REFERENCE = 4

def references(text):
    return set(REFERENCE.findall(text))

def expand_references(service, user_id, rows, top_k, max_refs=8,max_seed_rows=5):
    if type(max_seed_rows) is not int or not 1<=max_seed_rows<=100:raise ValueError('seed_row_limit')
    if type(max_refs) is not int or not 1<=max_refs<=8:raise ValueError('reference_limit')
    if type(top_k) is not int or top_k < 1 or len(rows) > top_k:
        raise ValueError('reference_row_limit')
    seen = {s for row in rows for s in row['source_message_ids']}
    seeds=[]
    owners={}
    # Read originals only, never parse generated presentation metadata.
    with service.db.connection() as con:
        for row_index, row in enumerate(rows[:max_seed_rows]):
            for mid in row['source_message_ids']:
                m=con.execute('SELECT content FROM messages WHERE user_id=? AND id=?',(user_id,mid)).fetchone()
                if m:
                    for ref in sorted(references(m['content'])):
                        owners.setdefault(ref, row_index)
                        if ref not in seeds:seeds.append(ref)
    seeds=seeds[:max_refs]
    extra=[]
    extra_refs=[]
    for ref in seeds:
        hits=service._pack_official_sources(user_id,service._official_search_unpacked({'user_id':user_id,'query':ref,'top_k':min(100,service.config.top_k_max)})['data'])
        with service.db.connection() as con:
            for hit in hits:
                for mid in hit['source_message_ids']:
                    if mid in seen:continue
                    m=con.execute('SELECT * FROM messages WHERE user_id=? AND id=?',(user_id,mid)).fetchone()
                    if not m or ref not in references(m['content']):continue
                    seen.add(mid)
                    extra_refs.append(ref)
                    extra.append({'id':mid,'content':m['content'],'source_message_ids':[mid],
                                  'created_at':m['created_at'],'score':0.0,
                                  'source_spans':[{'source_id':mid,'role':m['role'],
                                                   'message_time':source_message_time(m['ts_ms']),
                                                   'start':0,'end':len(m['content'])}]})
    # Preserve every baseline row and source even when top_k is already full.
    # An overflow source belongs to the original seed row that supplied its
    # exact reference, never to a newly discovered source (one-hop expansion).
    result = copy.deepcopy(rows)
    overflow_count = overflow_bytes = 0
    per_reference = {}
    for item, ref in zip(extra, extra_refs):
        if len(result) < top_k:
            result.append(item)
            continue
        span = item['source_spans'][0]
        label = ('[role=' + json.dumps(span['role'], ensure_ascii=False)
                 + '; message_time=' + json.dumps(span['message_time'])
                 + '; source=' + json.dumps(span['source_id']) + ']')
        addition = '\n' + label + '\n' + item['content']
        byte_count = len(addition.encode('utf-8'))
        if (overflow_count >= MAX_OVERFLOW_SOURCES
                or per_reference.get(ref, 0) >= MAX_OVERFLOW_PER_REFERENCE
                or overflow_bytes + byte_count > MAX_OVERFLOW_UTF8_BYTES):
            continue
        target = result[owners[ref]]
        start = len(target['content']) + len(label) + 2
        target['content'] += addition
        target['source_message_ids'].append(span['source_id'])
        target['source_spans'].append({**span, 'start': start,
                                      'end': start + len(item['content'])})
        overflow_count += 1
        overflow_bytes += byte_count
        per_reference[ref] = per_reference.get(ref, 0) + 1
    return result, seeds

