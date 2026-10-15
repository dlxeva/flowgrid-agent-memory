"""Untrusted selector output contract; no model, answer generation or memory writes."""
from dataclasses import dataclass

from .completion_adapter import ContractError, _decode
from .extraction import EvidenceSpan, ExtractionValidationError


class NumericSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class NumericSelection:
    status: str
    operation: str | None
    spans: tuple[EvidenceSpan, ...]


def parse_numeric_selection(raw, *, allowed_source_ids, allowed_operations):
    """Parse bounded exact JSON, with host-owned source/operation allowlists.

    Shape validity is not subject/event/unit/disjointness entailment. A model's
    unknown is merely its proposal, not a verified absence of evidence.
    """
    if (type(allowed_source_ids) not in (set, frozenset)
            or not 1 <= len(allowed_source_ids) <= 128
            or any(type(i) is not str or not 0 < len(i) <= 256 or i != i.strip()
                   or any(ord(c) < 32 or ord(c) == 127 for c in i) for i in allowed_source_ids)
            or type(allowed_operations) not in (set, frozenset)
            or not allowed_operations or not allowed_operations <= {'sum', 'difference'}):
        raise NumericSelectionError('invalid_host_selection_policy')
    if type(raw) is str:
        try:
            raw = raw.encode('utf-8')
        except UnicodeError:
            raise NumericSelectionError('invalid_selection_json') from None
    if type(raw) is not bytes or not 0 < len(raw) <= 16384:
        raise NumericSelectionError('invalid_selection_size')
    try:
        data = _decode(raw)
    except ContractError:
        raise NumericSelectionError('invalid_selection_json') from None
    if type(data) is not dict:
        raise NumericSelectionError('invalid_selection_shape')
    if data == {'status': 'unknown'}:
        return NumericSelection('unknown', None, ())
    if (set(data) != {'status', 'operation', 'operands'} or data['status'] != 'selected'
            or type(data['operation']) is not str or data['operation'] not in allowed_operations
            or type(data['operands']) is not list or not 2 <= len(data['operands']) <= 32
            or (data['operation'] == 'difference' and len(data['operands']) != 2)):
        raise NumericSelectionError('invalid_selection_shape')
    spans = []
    for item in data['operands']:
        if (type(item) is not dict or set(item) != {'source_event_id', 'start', 'end', 'quote'}
                or type(item['source_event_id']) is not str or item['source_event_id'] not in allowed_source_ids
                or type(item['quote']) is not str or not 0 < len(item['quote']) <= 64):
            raise NumericSelectionError('invalid_selection_operand')
        try:
            span = EvidenceSpan(**item)
        except ExtractionValidationError:
            raise NumericSelectionError('invalid_selection_operand') from None
        if span in spans:
            raise NumericSelectionError('duplicate_selection_operand')
        spans.append(span)
    return NumericSelection('selected', data['operation'], tuple(spans))


def apply_numeric_selection(memory, raw, *, principal, user_id, unit,
                            allowed_source_ids, allowed_operations,
                            requested_scope=None, enabled=False, allow_cross_session=False):
    """Host-owned policy, then current database re-read and deterministic result.

    No supplied free answer/value/unit/governance field can bypass calculation.
    Authentication itself is still the responsibility of the trusted host.
    """
    from .auth import (AuthorizationError, OPERATION_READ_AUDIT,
                       PERMISSION_EVIDENCE, authorize_operation)
    if type(enabled) is not bool or not enabled:
        raise NumericSelectionError('numeric_selector_disabled')
    authorize_operation(principal, user_id=user_id, requested_scope=requested_scope,
                        operation=OPERATION_READ_AUDIT)
    if PERMISSION_EVIDENCE not in principal.permissions:
        raise AuthorizationError()
    selection = parse_numeric_selection(raw, allowed_source_ids=allowed_source_ids,
                                        allowed_operations=allowed_operations)
    if selection.status == 'unknown':
        return {'status': 'unknown', 'reason': 'selector_abstained',
                'semantic_binding_verified': False, 'evidence_absence_verified': False,
                'state_actions': 0, 'default_changed': False}
    return memory.compose_numeric_evidence(principal=principal, user_id=user_id,
        spans=selection.spans, unit=unit, operation=selection.operation,
        requested_scope=requested_scope, enabled=True, allow_cross_session=allow_cross_session)
