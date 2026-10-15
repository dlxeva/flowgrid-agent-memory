"""Optional provenance-role policy for user-authored extraction adapters.

Not enabled by default. Does not infer semantic entailment, approve memories,
or discard individual failing proposals to manufacture successful batches.
"""
from .extraction import ExtractionValidationError,validate_proposals


def require_user_authored_evidence(request, proposals):
    """Require at least one quoted user event per draft; fail atomically.

Assistant context is allowed only alongside user evidence. A user quote alone
does not prove that the content follows from it or preserves modality.
"""
    validated=validate_proposals(request,proposals)
    events={event.id:event for event in request.raw_events}
    for proposal in validated:
        if not any(events[span.source_event_id].role=='user' for span in proposal.evidence_spans):
            raise ExtractionValidationError('user_authored_evidence_required')
    return validated
