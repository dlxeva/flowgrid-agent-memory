"""Opt-in exact source-bound arithmetic, not semantic evidence selection.

Trusted hosts supply already-authorized events, numeric spans and units. This
module cannot establish subject, assertion, disjointness or currency identity.
Results stay conditional; they never create or confirm a memory or an answer.
"""
from dataclasses import dataclass
from decimal import Decimal, localcontext
import hashlib
import re

from .extraction import EvidenceSpan
from .governance import RawEvent
from .numeric_evidence_adapter import NUMERAL, numeric_literal_tokens
from .temporal_comparison import WORDS

VERSION = 'flowgrid.source-numeric-composition/v1'


class NumericCompositionError(ValueError):
    pass


@dataclass(frozen=True)
class NumericOperand:
    source: RawEvent
    span: EvidenceSpan
    unit: str


def require(condition, code):
    if not condition:
        raise NumericCompositionError(code)


def resolve(operand):
    require(type(operand) is NumericOperand, 'typed_operand_required')
    source, span = operand.source, operand.span
    require(type(source) is RawEvent and type(span) is EvidenceSpan, 'typed_source_span_required')
    require(isinstance(source.content, str) and 0 < len(source.content) <= 65536,
            'invalid_source_content')
    require(isinstance(source.id, str) and bool(source.id)
            and isinstance(source.user_id, str) and bool(source.user_id)
            and isinstance(source.scope, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in source.scope.items()),
            'invalid_source_identity')
    require(span.source_event_id == source.id and 0 <= span.start < span.end <= len(source.content)
            and source.content[span.start:span.end] == span.quote, 'source_span_mismatch')
    require(isinstance(operand.unit, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,63}', operand.unit),
            'explicit_unit_required')
    quote = span.quote
    require(len(quote) <= 64, 'numeric_expression_too_large')
    # A slice of a larger literal must not masquerade as the original number.
    preceding, following = source.content[:span.start], source.content[span.end:]
    require(not preceding or not (preceding[-1].isalnum() or preceding[-1] in '+-_,.'),
            'incomplete_numeric_span')
    require(not following or not (following[0].isalnum() or following[0] == '_'
            or (following[0] in '.,' and len(following) > 1 and following[1].isdigit())),
            'incomplete_numeric_span')
    if quote.lower() in WORDS:
        value = Decimal(WORDS[quote.lower()])
    else:
        require(NUMERAL.fullmatch(quote) is not None, 'unsupported_numeric_expression')
        tokens = numeric_literal_tokens(quote)
        require(len(tokens) == 1, 'unsupported_numeric_expression')
        value = Decimal(quote.replace(',', ''))
    require(value.is_finite() and len(value.as_tuple().digits) <= 32
            and abs(value.as_tuple().exponent) <= 18, 'numeric_value_out_of_bounds')
    return value, {'source_event_id': source.id, 'user_id': source.user_id,
                   'scope': dict(source.scope), 'source_role': source.role,
                   'source_sha256': hashlib.sha256(source.content.encode()).hexdigest(),
                   'observed_at': source.observed_at, 'span': span.to_dict(),
                   'unit': operand.unit, 'value': format(value, 'f')}


def compose_numbers(operands, *, operation, enabled=False, allow_cross_session=False):
    require(type(enabled) is bool and enabled, 'numeric_composition_disabled')
    require(type(allow_cross_session) is bool, 'invalid_cross_session_policy')
    require(type(operands) in (list, tuple) and 2 <= len(operands) <= 32,
            'bounded_operand_sequence_required')
    require(operation in ('sum', 'difference') and
            (operation != 'difference' or len(operands) == 2), 'unsupported_operation')
    values, proofs = [], []
    for operand in operands:
        value, proof = resolve(operand)
        values.append(value); proofs.append(proof)
    def boundary(proof):
        return {k: v for k, v in proof['scope'].items()
                if not (allow_cross_session and k == 'session')}
    require(all(p['user_id'] == proofs[0]['user_id'] and boundary(p) == boundary(proofs[0])
                for p in proofs), 'cross_scope_composition_rejected')
    require(all(p['unit'] == proofs[0]['unit'] for p in proofs), 'unit_mismatch')
    spans, snapshots = set(), {}
    for proof in proofs:
        span = proof['span']; key = (proof['source_event_id'], span['start'], span['end'])
        require(key not in spans, 'duplicate_operand_span')
        spans.add(key)
        source_id = proof['source_event_id']
        snapshot = (proof['source_sha256'], proof['observed_at'], proof['source_role'])
        require(source_id not in snapshots or snapshots[source_id] == snapshot,
                'conflicting_source_snapshot')
        snapshots[source_id] = snapshot
    # Local high precision avoids global decimal-context rounding of valid inputs.
    with localcontext() as context:
        context.prec = 128
        result = sum(values, Decimal(0)) if operation == 'sum' else values[0] - values[1]
        if not result:
            result = Decimal(0)
        rendered = format(result, 'f').rstrip('0').rstrip('.') if '.' in format(result, 'f') else format(result, 'f')
    return {'version': VERSION, 'status': 'conditional_calculation',
            'operation': operation, 'unit': proofs[0]['unit'], 'value': rendered,
            'operands': proofs, 'semantic_binding_verified': False,
            'cross_session_policy': allow_cross_session,
            'disjointness_verified': False, 'authorization_verified': False,
            'state_actions': 0, 'default_changed': False}
