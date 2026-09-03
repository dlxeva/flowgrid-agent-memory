import json
import unittest

from aml_retriever import (
    AccessContext,
    DisclosurePolicy,
    ExtractionRequest,
    ExtractionValidationError,
    ExtractorInvocationError,
    FlowGridMemory,
    PERMISSION_AUDIT,
    PERMISSION_READ,
    QuoteAnchoredModelExtractor,
    RawEvent,
    build_model_extraction_prompt,
    quote_anchored_identity,
)


CATALOG = {
    "profile.home_city": {
        "memory_type": "fact",
        "description": "The city where the user explicitly says they currently live.",
    },
    "workflow.deploy_check": {
        "memory_type": "procedure",
        "description": "A durable deployment procedure explicitly required by the user.",
    },
}


def raw_event(content="我现在住在杭州🙂，这是长期信息。", *, event_id="raw-1"):
    return RawEvent(
        id=event_id,
        user_id="u1",
        event_type="message",
        role="user",
        content=content,
        observed_at="2026-09-02T00:00:00+00:00",
        recorded_at="2026-09-02T00:00:01+00:00",
        authority="user",
        scope={"user": "u1", "project": "demo"},
        source_locator=f"messages:{event_id}",
        source_message_id=f"msg-{event_id}",
    )


def make_request(event=None, identity=None):
    identity = identity or quote_anchored_identity(
        runner_config={"provider": "test", "model": "fixed"},
        key_catalog=CATALOG,
    )
    return ExtractionRequest(
        user_id="u1",
        idempotency_key="extract-1",
        raw_events=(event or raw_event(),),
        trusted_scope={"project": "demo"},
        extractor=identity,
    )


class TestQuoteAnchoredModelExtractor(unittest.TestCase):
    def make_extractor(self, runner):
        identity = quote_anchored_identity(
            runner_config={"provider": "test", "model": "fixed"},
            key_catalog=CATALOG,
        )
        return QuoteAnchoredModelExtractor(
            identity=identity,
            runner=runner,
            key_catalog=CATALOG,
        )

    def test_unique_unicode_quote_maps_to_exact_codepoint_span(self):
        event = raw_event()
        output = {
            "proposals": [
                {
                    "memory_key": "profile.home_city",
                    "memory_type": "fact",
                    "subject": "$user",
                    "content": "杭州",
                    "evidence_quotes": [
                        {"source_event_id": event.id, "quote": "杭州🙂"}
                    ],
                    "confidence": 0.98,
                }
            ]
        }
        extractor = self.make_extractor(lambda prompt: json.dumps(output, ensure_ascii=False))
        proposals = extractor.extract(make_request(event, extractor.identity))
        self.assertEqual(len(proposals), 1)
        span = proposals[0].evidence_spans[0]
        self.assertEqual(event.content[span.start:span.end], "杭州🙂")
        prompt = json.loads(build_model_extraction_prompt(
            make_request(event, extractor.identity), key_catalog=CATALOG
        ))
        self.assertIn("candidate", " ".join(prompt["rules"]))
        self.assertNotIn("user_id", prompt["output_schema"]["proposals"][0])

    def test_duplicate_quote_is_rejected(self):
        event = raw_event("杭州，然后还是杭州")
        extractor = self.make_extractor(
            lambda _prompt: {
                "proposals": [{
                    "memory_key": "profile.home_city",
                    "memory_type": "fact",
                    "subject": "$user",
                    "content": "杭州",
                    "evidence_quotes": [{"source_event_id": event.id, "quote": "杭州"}],
                }]
            }
        )
        with self.assertRaisesRegex(ExtractionValidationError, "model extractor output is invalid"):
            extractor.extract(make_request(event, extractor.identity))

    def test_unknown_key_and_governance_field_are_rejected(self):
        for mutation in (
            {"memory_key": "profile.unknown"},
            {"status": "confirmed"},
        ):
            proposal = {
                "memory_key": "profile.home_city",
                "memory_type": "fact",
                "subject": "$user",
                "content": "杭州",
                "evidence_quotes": [{"source_event_id": "raw-1", "quote": "杭州"}],
            }
            proposal.update(mutation)
            extractor = self.make_extractor(lambda _prompt, item=proposal: {"proposals": [item]})
            with self.assertRaisesRegex(ExtractionValidationError, "model extractor output is invalid"):
                extractor.extract(make_request(identity=extractor.identity))

    def test_timeout_is_sanitized(self):
        def timeout(_prompt):
            raise TimeoutError("source body and provider secret")

        extractor = self.make_extractor(timeout)
        with self.assertRaisesRegex(ExtractorInvocationError, "^extractor invocation timed out$"):
            extractor.extract(make_request(identity=extractor.identity))

    def test_identity_changes_with_runner_config(self):
        first = quote_anchored_identity(runner_config={"model": "a"}, key_catalog=CATALOG)
        second = quote_anchored_identity(runner_config={"model": "b"}, key_catalog=CATALOG)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_facade_persists_candidate_and_current_abstains(self):
        memory = FlowGridMemory(db_path=":memory:")
        try:
            ingested = memory.ingest_raw_events(
                request_id="r1",
                user_id="u1",
                session_id="s1",
                trusted_scope={"project": "demo"},
                messages=[{
                    "role": "user",
                    "content": "我现在住在杭州。",
                    "timestamp": 1_780_000_000_000,
                }],
            )
            output = {
                "proposals": [{
                    "memory_key": "profile.home_city",
                    "memory_type": "fact",
                    "subject": "$user",
                    "content": "杭州",
                    "evidence_quotes": [{
                        "source_event_id": ingested.raw_event_ids[0],
                        "quote": "我现在住在杭州。",
                    }],
                }]
            }
            extractor = self.make_extractor(lambda _prompt: output)
            receipt = memory.extract_candidates(
                user_id="u1",
                raw_event_ids=ingested.raw_event_ids,
                idempotency_key="natural-1",
                trusted_scope={"project": "demo"},
                extractor=extractor,
            )
            self.assertEqual(receipt.proposal_count, 1)
            access = AccessContext(
                principal_id="owner",
                authority="owner",
                scopes={"user": "u1", "project": "demo"},
                permissions=frozenset({PERMISSION_READ, PERMISSION_AUDIT}),
                purpose="debug",
                allowed_users=frozenset({"u1"}),
            )
            policy = DisclosurePolicy(allowed_audit_purposes=frozenset({"debug"}))
            audit = memory.query_audit(
                user_id="u1",
                access_context=access,
                memory_key="profile.home_city",
                scope={"project": "demo"},
                disclosure_policy=policy,
            )
            current = memory.query_current(
                user_id="u1",
                access_context=access,
                memory_key="profile.home_city",
                scope={"project": "demo"},
            )
            self.assertTrue(audit.allowed)
            self.assertEqual([record.status for record in audit.state.records], ["candidate"])
            self.assertTrue(current.allowed)
            self.assertTrue(current.state.abstain)
            self.assertEqual(current.state.records, [])
        finally:
            memory.close()


if __name__ == "__main__":
    unittest.main()
