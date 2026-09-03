"""Adversarial contract tests for the RawEvent-to-candidate pipeline."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest import mock

from aml_retriever.api import MemoryService
from aml_retriever.config import RetrieverConfig
from aml_retriever.extraction import (
    DIRECTIVE_PREFIX,
    CallableMemoryExtractor,
    EvidenceSpan,
    ExtractionConflict,
    ExtractionRequest,
    ExtractionValidationError,
    ExtractorIdentity,
    ExtractorInvocationError,
    ProposalDraft,
)
from aml_retriever.retriever import RetrieverDB
from aml_retriever.governance import GovernanceError


def _span(event, quote: str) -> dict:
    start = event.content.index(quote)
    return {
        "source_event_id": event.id,
        "start": start,
        "end": start + len(quote),
        "quote": quote,
    }


def _proposal(event, *, key="profile.city", memory_type="fact", quote="杭州") -> dict:
    return {
        "memory_key": key,
        "memory_type": memory_type,
        "subject": "user-profile",
        "content": quote,
        "evidence_spans": [_span(event, quote)],
        "confidence": 0.99,
    }


def _directive_proposal(event, **kwargs) -> dict:
    value = _proposal(event, **kwargs)
    value.pop("evidence_spans")
    return value


class ExtractionCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.service = MemoryService(RetrieverConfig(db_path=self.path))
        self._counter = 0

    def tearDown(self):
        self.service.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def add_event(self, content: str, *, user_id="u1", role="user"):
        self._counter += 1
        request_id = f"raw-{user_id}-{self._counter}"
        self.service.official_add(
            {
                "request_id": request_id,
                "user_id": user_id,
                "session_id": f"s-{self._counter}",
                "messages": [{"role": role, "content": content}],
            }
        )
        return next(
            event
            for event in self.service.list_raw_events(user_id, limit=10_000)
            if event.content == content
        )

    def add_directive(self, proposals: list[dict], *, user_id="u1"):
        content = DIRECTIVE_PREFIX + "\n" + json.dumps(
            {"proposals": proposals}, ensure_ascii=False, sort_keys=True
        )
        return self.add_event(content, user_id=user_id)

    def compile(self, events, *, key="compile-1", scope=None, extractor=None):
        return self.service.compile_events(
            user_id="u1",
            raw_event_ids=[event.id for event in events],
            idempotency_key=key,
            trusted_scope=scope or {},
            extractor=extractor,
        )


class TestDirectiveAndGovernance(ExtractionCase):
    def test_five_memory_types_are_candidate_only_and_auditable(self):
        quotes = {
            "fact": "杭州",
            "preference": "少糖",
            "event": "周五演示",
            "procedure": "先跑测试",
            "judgment": "风险偏高",
        }
        evidence = self.add_event("；".join(quotes.values()))
        proposals = [
            _directive_proposal(
                evidence,
                key=f"kind.{memory_type}",
                memory_type=memory_type,
                quote=quote,
            )
            for memory_type, quote in quotes.items()
        ]
        directive = self.add_directive(proposals)

        # AML Add is still raw-only; extraction is explicitly invoked.
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM memory_records")[0][0], 0)
        receipt = self.compile(
            [evidence, directive],
            scope={"project": "demo"},
        )
        self.assertEqual(receipt.status, "success")
        self.assertEqual(receipt.proposal_count, 5)
        self.assertEqual(len(receipt.request_digest), 64)
        self.assertEqual(len(receipt.extractor_fingerprint), 64)
        self.assertEqual(len(receipt.record_ids), 5)

        rows = self.service.db.query(
            "SELECT * FROM memory_records ORDER BY memory_type"
        )
        self.assertEqual({row["memory_type"] for row in rows}, set(quotes))
        self.assertTrue(all(row["status"] == "candidate" for row in rows))
        self.assertTrue(all(row["authority"] == "user" for row in rows))
        self.assertTrue(all(json.loads(row["scope_json"])["project"] == "demo" for row in rows))
        self.assertTrue(all(row["confirmed_by"] is None for row in rows))
        self.assertTrue(all(row["supersedes_record_id"] is None for row in rows))

        current = self.service.search_governed(
            user_id="u1",
            memory_key="kind.fact",
            scope={"project": "demo"},
        )
        self.assertEqual(current.current_status, "unknown")
        self.assertTrue(current.abstain)
        self.assertTrue(current.owner_gate_required)
        self.assertEqual(current.records, [])

        audit = self.service.search_governed(
            user_id="u1",
            memory_key="kind.fact",
            mode="audit",
            scope={"project": "demo"},
        )
        self.assertEqual([record.status for record in audit.records], ["candidate"])
        self.assertEqual([event.id for event in audit.raw_events], [directive.id])
        self.assertEqual(audit.raw_events[0].content, directive.content)
        self.assertEqual(len(audit.state_events), 1)

        origins = self.service.db.query("SELECT * FROM proposal_origins")
        self.assertEqual(len(origins), 5)
        stored_spans = json.loads(origins[0]["evidence_spans_json"])
        self.assertIn("quote_sha256", stored_spans[0])
        self.assertNotIn("quote", stored_spans[0])

    def test_ordinary_free_text_is_successful_zero_proposal_and_idempotent(self):
        event = self.add_event("这是一条普通自然语言，不应该被假装理解")
        first = self.compile([event], key="zero")
        second = self.compile([event], key="zero")
        self.assertEqual(first.proposal_count, 0)
        self.assertEqual(first.record_ids, ())
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(first.request_digest, second.request_digest)
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM extraction_receipts")[0][0], 1)
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM memory_records")[0][0], 0)

    def test_authority_is_derived_conservatively_from_same_user_sources(self):
        user_event = self.add_event("用户说杭州", role="user")
        agent_event = self.add_event("助手重复杭州", role="assistant")
        spans = [_span(user_event, "杭州"), _span(agent_event, "杭州")]
        identity = ExtractorIdentity("host", "1", "mixed-authority-test")

        def produce(_request):
            proposal = _proposal(user_event)
            proposal["evidence_spans"] = spans
            return [proposal]

        self.compile(
            [user_event, agent_event],
            scope={"agent": "a1"},
            extractor=CallableMemoryExtractor(identity, produce),
        )
        row = self.service.db.query("SELECT authority,scope_json FROM memory_records")[0]
        self.assertEqual(row["authority"], "unknown")
        self.assertEqual(json.loads(row["scope_json"]), {"agent": "a1", "user": "u1"})

    def test_user_subject_alias_closes_direct_preference_owner_gate(self):
        evidence = self.add_event("少糖")
        proposal = _directive_proposal(
            evidence,
            key="preference.sugar",
            memory_type="preference",
            quote="少糖",
        )
        proposal["subject"] = "$user"
        directive = self.add_directive([proposal])
        receipt = self.compile([directive], key="preference-user-alias")
        row = self.service.db.query(
            "SELECT subject,status,authority FROM memory_records WHERE id=?",
            (receipt.record_ids[0],),
        )[0]
        self.assertEqual(row["subject"], "u1")
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(row["authority"], "user")

        confirmed = self.service.transition_memory(
            record_id=receipt.record_ids[0],
            target_status="confirmed",
            actor="owner-1",
            actor_authority="owner",
            reason="owner verified direct user preference",
            user_id="u1",
        )
        self.assertEqual(confirmed.status, "confirmed")

        other = _directive_proposal(
            evidence,
            key="preference.entity",
            memory_type="preference",
            quote="少糖",
        )
        other["subject"] = "team-a"
        other_directive = self.add_directive([other])
        other_receipt = self.compile(
            [other_directive],
            key="preference-literal-entity",
        )
        with self.assertRaises(GovernanceError):
            self.service.transition_memory(
                record_id=other_receipt.record_ids[0],
                target_status="confirmed",
                actor="owner-1",
                actor_authority="owner",
                reason="literal non-user subject stays blocked",
                user_id="u1",
            )

    def test_official_add_and_search_wire_shape_is_unchanged_and_never_auto_extracts(self):
        body = self.service.official_add(
            {
                "request_id": "official-still-raw",
                "user_id": "u1",
                "session_id": "official",
                "messages": [{"role": "user", "content": "普通 AML 正文"}],
            }
        )
        self.assertEqual(set(body), {"success", "request_id", "user_id", "session_id"})
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM memory_records")[0][0], 0)
        search = self.service.official_search({"query": "AML", "user_id": "u1", "top_k": 3})
        self.assertEqual(set(search), {"data"})
        self.assertTrue(search["data"])


class TestStrictValidationAndAtomicity(ExtractionCase):
    def _assert_no_derived_rows(self):
        for table in (
            "memory_records",
            "memory_state_events",
            "proposal_origins",
            "extraction_receipts",
        ):
            self.assertEqual(
                self.service.db.query(f"SELECT COUNT(*) FROM {table}")[0][0],
                0,
                table,
            )

    def test_forbidden_governance_field_rejects_whole_directive(self):
        evidence = self.add_event("杭州")
        proposal = _directive_proposal(evidence)
        proposal["status"] = "confirmed"
        directive = self.add_directive([proposal])
        with self.assertRaises(ExtractionValidationError):
            self.compile([evidence, directive], key="forbidden")
        self._assert_no_derived_rows()
        self.assertEqual(len(self.service.list_raw_events("u1")), 2)

    def test_directive_cannot_claim_another_batch_event_as_provenance(self):
        evidence = self.add_event("杭州")
        proposal = _directive_proposal(evidence)
        proposal["evidence_spans"] = [_span(evidence, "杭州")]
        directive = self.add_directive([proposal])
        with self.assertRaises(ExtractionValidationError):
            self.compile([evidence, directive], key="directive-source-forgery")
        self._assert_no_derived_rows()

    def test_callable_cannot_forge_scope_authority_or_record_id(self):
        evidence = self.add_event("杭州")
        identity = ExtractorIdentity("host", "1", "malicious-output-test")

        def malicious(_request):
            value = _proposal(evidence)
            value.update({"authority": "owner", "scope": {"tenant": "x"}, "record_id": "x"})
            return [value]

        extractor = CallableMemoryExtractor(identity, malicious)
        with self.assertRaises(ExtractionValidationError):
            self.compile([evidence], key="callable-forbidden", extractor=extractor)
        self._assert_no_derived_rows()

    def test_fake_source_and_bad_span_each_reject_the_entire_batch(self):
        evidence = self.add_event("杭州")
        identity = ExtractorIdentity("host", "1", "span-validation-test")

        def fake_output(_request):
            valid = _proposal(evidence, key="valid")
            fake = _proposal(evidence, key="fake")
            fake["evidence_spans"][0]["source_event_id"] = "raw_not_in_batch"
            return [valid, fake]

        with self.assertRaises(ExtractionValidationError):
            self.compile(
                [evidence],
                key="fake-source",
                extractor=CallableMemoryExtractor(identity, fake_output),
            )
        self._assert_no_derived_rows()

        def bad_output(_request):
            bad = _proposal(evidence)
            bad["evidence_spans"][0]["quote"] = "上海"
            return [bad]

        with self.assertRaises(ExtractionValidationError):
            self.compile(
                [evidence],
                key="bad-span",
                extractor=CallableMemoryExtractor(identity, bad_output),
            )
        self._assert_no_derived_rows()

    def test_injected_callable_cannot_mutate_trusted_request_snapshot(self):
        evidence = self.add_event("杭州")
        identity = ExtractorIdentity("host", "1", "mutation-test")

        def mutate(request):
            # Bypass the FrozenDict override deliberately; the post-call exact
            # digest check is the second line of defense.
            dict.__setitem__(request.trusted_scope, "project", "forged")
            dict.__setitem__(request.raw_events[0].scope, "project", "forged")
            return []

        with self.assertRaises(ExtractionValidationError) as ctx:
            self.compile(
                [evidence],
                key="mutation",
                scope={"project": "trusted"},
                extractor=CallableMemoryExtractor(identity, mutate),
            )
        self.assertIn("mutated", str(ctx.exception))
        self._assert_no_derived_rows()

    def test_privacy_erase_and_readd_same_raw_id_fails_exact_snapshot_check(self):
        evidence = self.add_event("旧证据正文")
        other_service = MemoryService(RetrieverConfig(db_path=self.path))
        identity = ExtractorIdentity("host", "1", "erase-readd-race-test")

        def replace_evidence(request):
            old = request.raw_events[0]
            other_service.delete_user("u1")
            other_service.official_add(
                {
                    "request_id": "race-new",
                    "user_id": "u1",
                    "session_id": "s-1",
                    "messages": [{"role": "user", "content": "新证据正文"}],
                }
            )
            new = other_service.list_raw_events("u1")[0]
            self.assertEqual(new.id, old.id, "test must exercise deterministic ID reuse")
            return [
                ProposalDraft(
                    memory_key="race.fact",
                    memory_type="fact",
                    subject="$user",
                    content="旧证据正文",
                    evidence_spans=(
                        EvidenceSpan(old.id, 0, len(old.content), old.content),
                    ),
                )
            ]

        try:
            with self.assertRaises(ExtractionValidationError) as ctx:
                self.compile(
                    [evidence],
                    key="erase-readd-race",
                    extractor=CallableMemoryExtractor(identity, replace_evidence),
                )
            self.assertEqual(str(ctx.exception), "source evidence changed before persistence")
            self._assert_no_derived_rows()
            live = self.service.list_raw_events("u1")
            self.assertEqual([(event.id, event.content) for event in live], [(evidence.id, "新证据正文")])
        finally:
            other_service.close()

    def test_lower_level_persist_rejects_forged_raw_authority_snapshot(self):
        actual = self.add_event("助手来源", role="assistant")
        forged = replace(actual, authority="user")
        identity = ExtractorIdentity("host", "1", "forged-snapshot-test")
        request = ExtractionRequest(
            user_id="u1",
            idempotency_key="forged-snapshot",
            raw_events=(forged,),
            trusted_scope={},
            extractor=identity,
        )
        proposal = ProposalDraft(
            memory_key="forged.preference",
            memory_type="preference",
            subject="$user",
            content="伪造用户偏好",
            evidence_spans=(
                EvidenceSpan(forged.id, 0, len(forged.content), forged.content),
            ),
        )
        with self.assertRaises(ExtractionValidationError):
            self.service.db.persist_extraction(
                request=request,
                proposals=(proposal,),
            )
        self._assert_no_derived_rows()

    def test_malicious_proposal_subclass_cannot_mutate_authority_during_clone(self):
        actual = self.add_event("助手来源", role="assistant")
        identity = ExtractorIdentity("host", "1", "proposal-subclass-attack")
        request = ExtractionRequest(
            user_id="u1",
            idempotency_key="proposal-subclass-attack",
            raw_events=(actual,),
            trusted_scope={},
            extractor=identity,
        )
        attack = {"done": False}

        class MutatingProposal(ProposalDraft):
            def __getattribute__(self, name):
                if name == "memory_key" and not attack["done"]:
                    attack["done"] = True
                    object.__setattr__(request.raw_events[0], "authority", "user")
                return super().__getattribute__(name)

        proposal = MutatingProposal(
            memory_key="attack.fact",
            memory_type="fact",
            subject="$user",
            content="不应落库",
            evidence_spans=(
                EvidenceSpan(actual.id, 0, len(actual.content), actual.content),
            ),
        )
        with self.assertRaises(ExtractionValidationError):
            self.service.db.persist_extraction(
                request=request,
                proposals=(proposal,),
            )
        self.assertTrue(attack["done"])
        self._assert_no_derived_rows()
        self.assertEqual(
            self.service.db.query(
                "SELECT authority FROM raw_events WHERE id=?", (actual.id,)
            )[0]["authority"],
            "agent",
        )

    def test_cross_user_source_is_rejected_without_creating_receipt(self):
        other = self.add_event("其他用户正文", user_id="u2")
        with self.assertRaises(ExtractionValidationError):
            self.service.compile_events(
                user_id="u1",
                raw_event_ids=[other.id],
                idempotency_key="cross-user",
            )
        self._assert_no_derived_rows()

    def test_mid_persist_failure_rolls_back_record_state_origin_and_receipt(self):
        evidence = self.add_event("杭州；少糖")
        directive = self.add_directive(
            [
                _directive_proposal(evidence, key="first", quote="杭州"),
                _directive_proposal(
                    evidence,
                    key="second",
                    memory_type="preference",
                    quote="少糖",
                ),
            ]
        )
        from aml_retriever.compiler import governed as compiler_governed

        real_create = compiler_governed.create_memory_record
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic persistence failure")
            return real_create(*args, **kwargs)

        with mock.patch(
            "aml_retriever.compiler.governed.create_memory_record",
            side_effect=fail_second,
        ):
            with self.assertRaises(RuntimeError):
                self.compile([evidence, directive], key="rollback")
        self._assert_no_derived_rows()
        self.assertEqual(len(self.service.list_raw_events("u1")), 2)


class TestIdempotencyAndCallable(ExtractionCase):
    def _callable(self, identity, calls):
        def function(request):
            calls.append(request.digest)
            event = request.raw_events[0]
            quote = event.content
            return [
                ProposalDraft(
                    memory_key="callable.fact",
                    memory_type="fact",
                    subject="user-profile",
                    content=quote,
                    evidence_spans=(EvidenceSpan(event.id, 0, len(quote), quote),),
                    confidence=1.0,
                )
            ]

        return CallableMemoryExtractor(identity, function)

    def test_same_key_same_digest_returns_same_ids_without_reinvocation(self):
        event = self.add_event("确定事实")
        identity = ExtractorIdentity("host", "1", "deterministic-test")
        calls = []
        extractor = self._callable(identity, calls)
        first = self.compile([event], key="idem", extractor=extractor)
        second = self.compile([event], key="idem", extractor=extractor)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.record_ids, second.record_ids)
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM memory_records")[0][0], 1)

    def test_same_key_different_digest_conflicts_before_callable(self):
        event = self.add_event("确定事实")
        identity = ExtractorIdentity("host", "1", "conflict-test")
        calls = []
        extractor = self._callable(identity, calls)
        self.compile([event], key="idem-conflict", scope={"project": "a"}, extractor=extractor)
        with self.assertRaises(ExtractionConflict):
            self.compile(
                [event],
                key="idem-conflict",
                scope={"project": "b"},
                extractor=extractor,
            )
        self.assertEqual(len(calls), 1)

    def test_exception_and_timeout_leave_no_receipt_and_are_retryable(self):
        secret = "RAW-MODEL-OUTPUT-MUST-NOT-LEAK"
        event = self.add_event("可重试正文")
        identity = ExtractorIdentity("host", "1", "retry-test")

        def fail(_request):
            raise RuntimeError(secret)

        with self.assertRaises(ExtractorInvocationError) as ctx:
            self.compile(
                [event],
                key="retry-exception",
                extractor=CallableMemoryExtractor(identity, fail),
            )
        self.assertNotIn(secret, str(ctx.exception))
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM extraction_receipts")[0][0], 0)
        calls = []
        recovered = self.compile(
            [event],
            key="retry-exception",
            extractor=self._callable(identity, calls),
        )
        self.assertEqual(recovered.proposal_count, 1)

        timeout_identity = ExtractorIdentity("host", "1", "timeout-test")

        def host_timeout(_request):
            # The host is responsible for actual cancellation/limiting, then
            # reports the completed timeout outcome across this boundary.
            raise TimeoutError(secret)

        with self.assertRaises(ExtractorInvocationError) as timeout_ctx:
            self.compile(
                [event],
                key="retry-timeout",
                extractor=CallableMemoryExtractor(timeout_identity, host_timeout),
            )
        self.assertNotIn(secret, str(timeout_ctx.exception))
        self.assertEqual(
            self.service.db.query(
                "SELECT COUNT(*) FROM extraction_receipts WHERE idempotency_key='retry-timeout'"
            )[0][0],
            0,
        )
        retry = self.compile(
            [event],
            key="retry-timeout",
            extractor=CallableMemoryExtractor(timeout_identity, lambda _request: []),
        )
        self.assertEqual(retry.proposal_count, 0)

    def test_custom_protocol_exception_cannot_smuggle_raw_content(self):
        secret = "CUSTOM-EXTRACTOR-RAW-CONTENT-SENTINEL"
        event = self.add_event(secret)

        class MaliciousExtractor:
            identity = ExtractorIdentity("custom", "1", "exception-smuggling")

            def extract(self, request):
                raise ExtractionValidationError(request.raw_events[0].content)

        with self.assertRaises(ExtractorInvocationError) as ctx:
            self.compile(
                [event],
                key="custom-exception-smuggling",
                extractor=MaliciousExtractor(),
            )
        self.assertEqual(str(ctx.exception), "extractor invocation failed")
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual(
            self.service.db.query("SELECT COUNT(*) FROM extraction_receipts")[0][0],
            0,
        )

    def test_custom_protocol_control_flow_cannot_escape_or_smuggle_raw_content(self):
        secret = "CUSTOM-EXTRACTOR-CONTROL-FLOW-SENTINEL"
        event = self.add_event(secret)

        for error_type in (SystemExit, KeyboardInterrupt):
            with self.subTest(error_type=error_type.__name__):
                class ControlFlowExtractor:
                    identity = ExtractorIdentity(
                        "custom",
                        "1",
                        f"control-flow-{error_type.__name__.lower()}",
                    )

                    def extract(self, request):
                        raise error_type(request.raw_events[0].content)

                with self.assertRaises(ExtractorInvocationError) as ctx:
                    self.compile(
                        [event],
                        key=f"custom-control-flow-{error_type.__name__.lower()}",
                        extractor=ControlFlowExtractor(),
                    )
                self.assertEqual(str(ctx.exception), "extractor invocation failed")
                self.assertNotIn(secret, str(ctx.exception))
                self.assertIsNone(ctx.exception.__cause__)

        self.assertEqual(
            self.service.db.query("SELECT COUNT(*) FROM extraction_receipts")[0][0],
            0,
        )

    def test_exact_duplicate_proposals_are_deduplicated_first_wins(self):
        event = self.add_event("唯一事实")
        identity = ExtractorIdentity("host", "1", "exact-dedupe-test")

        def duplicate(request):
            source = request.raw_events[0]
            proposal = ProposalDraft(
                memory_key="dedupe.fact",
                memory_type="fact",
                subject="$user",
                content="唯一事实",
                evidence_spans=(
                    EvidenceSpan(source.id, 0, len(source.content), source.content),
                ),
            )
            return [proposal, proposal]

        receipt = self.compile(
            [event],
            key="exact-dedupe",
            extractor=CallableMemoryExtractor(identity, duplicate),
        )
        self.assertEqual(receipt.proposal_count, 1)
        self.assertEqual(len(receipt.record_ids), 1)
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM memory_records")[0][0], 1)
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM proposal_origins")[0][0], 1)


class TestConcurrencyAndPrivacy(ExtractionCase):
    def test_concurrent_same_request_persists_one_candidate_set(self):
        event = self.add_event("并发事实")
        other_service = MemoryService(RetrieverConfig(db_path=self.path))
        barrier = threading.Barrier(2)
        identity = ExtractorIdentity("host", "1", "concurrency-test")
        results = []
        errors = []

        def function(request):
            barrier.wait(timeout=5)
            source = request.raw_events[0]
            return [
                ProposalDraft(
                    memory_key="concurrent.fact",
                    memory_type="fact",
                    subject="user-profile",
                    content="并发事实",
                    evidence_spans=(EvidenceSpan(source.id, 0, len(source.content), source.content),),
                )
            ]

        def run(service):
            try:
                results.append(
                    service.compile_events(
                        user_id="u1",
                        raw_event_ids=[event.id],
                        idempotency_key="concurrent",
                        extractor=CallableMemoryExtractor(identity, function),
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(service,)) for service in (self.service, other_service)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        other_service.close()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].record_ids, results[1].record_ids)
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM memory_records")[0][0], 1)
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM memory_state_events")[0][0], 1)
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM proposal_origins")[0][0], 1)
        self.assertEqual(self.service.db.query("SELECT COUNT(*) FROM extraction_receipts")[0][0], 1)

    def test_privacy_erase_and_purge_remove_receipts_and_origins(self):
        event = self.add_event("删除事实")
        calls = []
        extractor = self._make_simple_extractor(calls, "erase")
        self.compile([event], key="erase", extractor=extractor)
        report = self.service.delete_user("u1")
        self.assertEqual(report["deleted_extraction_receipts"], 1)
        self.assertEqual(report["deleted_proposal_origins"], 1)
        for table in (
            "messages",
            "raw_events",
            "memory_records",
            "memory_state_events",
            "proposal_origins",
            "extraction_receipts",
        ):
            self.assertEqual(self.service.db.query(f"SELECT COUNT(*) FROM {table}")[0][0], 0)

        event2 = self.add_event("清库事实")
        self.compile([event2], key="purge", extractor=self._make_simple_extractor([], "purge"))
        self.service.db.purge_all()
        for table in (
            "messages",
            "raw_events",
            "memory_records",
            "memory_state_events",
            "proposal_origins",
            "extraction_receipts",
        ):
            self.assertEqual(self.service.db.query(f"SELECT COUNT(*) FROM {table}")[0][0], 0)

    @staticmethod
    def _make_simple_extractor(calls, implementation):
        identity = ExtractorIdentity("host", "1", implementation)

        def function(request):
            calls.append(1)
            event = request.raw_events[0]
            return [
                ProposalDraft(
                    memory_key="privacy.fact",
                    memory_type="fact",
                    subject="user-profile",
                    content=event.content,
                    evidence_spans=(EvidenceSpan(event.id, 0, len(event.content), event.content),),
                )
            ]

        return CallableMemoryExtractor(identity, function)


class TestExtractionSchemaGate(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def test_existing_v1_reopens_and_future_version_fails_closed(self):
        service = MemoryService(RetrieverConfig(db_path=self.path))
        self.assertEqual(
            service.db.query(
                "SELECT value FROM extraction_meta WHERE key='schema_version'"
            )[0]["value"],
            "1",
        )
        service.close()

        reopened = MemoryService(RetrieverConfig(db_path=self.path))
        reopened.close()
        con = sqlite3.connect(self.path)
        con.execute(
            "UPDATE extraction_meta SET value='2' WHERE key='schema_version'"
        )
        con.commit()
        con.close()
        with self.assertRaises(ExtractionValidationError):
            MemoryService(RetrieverConfig(db_path=self.path))

    def test_two_instances_initialize_one_schema_version_safely(self):
        barrier = threading.Barrier(3)
        created = []
        errors = []

        def initialize():
            try:
                barrier.wait(timeout=5)
                created.append(RetrieverDB(RetrieverConfig(db_path=self.path)))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=initialize) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)
        try:
            self.assertEqual(errors, [])
            self.assertEqual(len(created), 2)
            rows = created[0].query(
                "SELECT key,value FROM extraction_meta WHERE key='schema_version'"
            )
            self.assertEqual([(row["key"], row["value"]) for row in rows], [("schema_version", "1")])
            self.assertEqual(
                created[0].query(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                    "AND name IN ('extraction_receipts','proposal_origins')"
                )[0][0],
                2,
            )
        finally:
            for db in created:
                db.close()

    def test_same_name_noop_immutability_trigger_is_rejected(self):
        service = MemoryService(RetrieverConfig(db_path=self.path))
        service.close()
        con = sqlite3.connect(self.path)
        con.execute("DROP TRIGGER extraction_receipts_no_update")
        con.execute(
            "CREATE TRIGGER extraction_receipts_no_update "
            "BEFORE UPDATE ON extraction_receipts BEGIN SELECT 1; END"
        )
        con.commit()
        con.close()
        with self.assertRaises(ExtractionValidationError) as ctx:
            MemoryService(RetrieverConfig(db_path=self.path))
        self.assertIn("immutability guards", str(ctx.exception))

    def test_receipt_table_without_composite_primary_key_is_rejected(self):
        service = MemoryService(RetrieverConfig(db_path=self.path))
        service.close()
        con = sqlite3.connect(self.path)
        trigger_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='extraction_receipts_no_update'"
        ).fetchone()[0]
        con.execute("DROP TRIGGER extraction_receipts_no_update")
        con.execute("DROP INDEX idx_extraction_receipt_digest")
        con.execute(
            "ALTER TABLE extraction_receipts RENAME TO extraction_receipts_old"
        )
        con.execute(
            """CREATE TABLE extraction_receipts(
            user_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            extractor_fingerprint TEXT NOT NULL,
            output_fingerprint TEXT NOT NULL,
            proposal_count INTEGER NOT NULL,
            record_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL
        )"""
        )
        con.execute("DROP TABLE extraction_receipts_old")
        con.execute(trigger_sql)
        con.commit()
        con.close()

        with self.assertRaises(ExtractionValidationError) as ctx:
            MemoryService(RetrieverConfig(db_path=self.path))
        self.assertEqual(str(ctx.exception), "extraction schema layout is incompatible")

    def test_origin_table_missing_critical_column_is_rejected(self):
        service = MemoryService(RetrieverConfig(db_path=self.path))
        service.close()
        con = sqlite3.connect(self.path)
        trigger_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='proposal_origins_no_update'"
        ).fetchone()[0]
        con.execute("DROP TRIGGER proposal_origins_no_update")
        con.execute("DROP INDEX idx_proposal_origins_receipt")
        con.execute("ALTER TABLE proposal_origins RENAME TO proposal_origins_old")
        con.execute(
            """CREATE TABLE proposal_origins(
            record_id TEXT NOT NULL PRIMARY KEY,
            user_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            extractor_fingerprint TEXT NOT NULL,
            proposal_index INTEGER NOT NULL,
            proposal_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
        )
        con.execute("DROP TABLE proposal_origins_old")
        con.execute(trigger_sql)
        con.commit()
        con.close()

        with self.assertRaises(ExtractionValidationError) as ctx:
            MemoryService(RetrieverConfig(db_path=self.path))
        self.assertEqual(str(ctx.exception), "extraction schema layout is incompatible")

    def test_origin_table_with_weakened_not_null_is_rejected(self):
        service = MemoryService(RetrieverConfig(db_path=self.path))
        service.close()
        con = sqlite3.connect(self.path)
        trigger_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='proposal_origins_no_update'"
        ).fetchone()[0]
        con.execute("DROP TRIGGER proposal_origins_no_update")
        con.execute("DROP INDEX idx_proposal_origins_receipt")
        con.execute("ALTER TABLE proposal_origins RENAME TO proposal_origins_old")
        con.execute(
            """CREATE TABLE proposal_origins(
            record_id TEXT NOT NULL PRIMARY KEY,
            user_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            extractor_fingerprint TEXT NOT NULL,
            proposal_index INTEGER NOT NULL,
            proposal_fingerprint TEXT NOT NULL,
            evidence_spans_json TEXT,
            created_at TEXT NOT NULL
        )"""
        )
        con.execute("DROP TABLE proposal_origins_old")
        con.execute(trigger_sql)
        con.commit()
        con.close()

        with self.assertRaises(ExtractionValidationError) as ctx:
            MemoryService(RetrieverConfig(db_path=self.path))
        self.assertEqual(str(ctx.exception), "extraction schema layout is incompatible")


if __name__ == "__main__":
    unittest.main()
