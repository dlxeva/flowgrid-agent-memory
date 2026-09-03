"""Access control and deterministic governed-context compiler invariants."""
import os
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from aml_retriever.access import (
    PERMISSION_AUDIT,
    PERMISSION_READ,
    AccessContext,
    DisclosurePolicy,
)
from aml_retriever.api import MemoryService
from aml_retriever.config import RetrieverConfig
from aml_retriever.context import ContextCompiler
from aml_retriever.governance import CurrentStateResult


class ExactCharacterCounter:
    is_exact = True

    def count_tokens(self, text):
        return len(text)


class InexactCounter:
    is_exact = False

    def count_tokens(self, text):
        return len(text)


class BrokenCounter:
    is_exact = True

    def count_tokens(self, text):
        raise RuntimeError("tokenizer unavailable")


class BoolCounter:
    is_exact = True

    def count_tokens(self, text):
        return True


class ContextCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.service = MemoryService(RetrieverConfig(db_path=self.path))
        self.serial = 0

    def tearDown(self):
        self.service.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def access(
        self,
        *,
        users=("u1",),
        scopes=None,
        permissions=(PERMISSION_READ,),
        purpose=None,
    ):
        return AccessContext(
            principal_id="trusted-service",
            authority="service",
            scopes=scopes or {},
            permissions=frozenset(permissions),
            purpose=purpose,
            allowed_users=frozenset(users),
        )

    def add_event(self, content, *, user_id="u1", session_id="s1", role="user"):
        self.serial += 1
        added = self.service.add(
            request_id=f"req-{self.serial}",
            user_id=user_id,
            session_id=session_id,
            messages=[
                {
                    "role": role,
                    "content": content,
                    "timestamp": 1_700_000_000_000 + self.serial,
                }
            ],
        )
        message_id = added.message_ids[0]
        return next(
            event
            for event in self.service.list_raw_events(user_id)
            if event.source_message_id == message_id
        )

    def propose(self, content, *, key="profile.city", scope=None, user_id="u1"):
        event = self.add_event(content, user_id=user_id)
        return self.service.propose_memory(
            user_id=user_id,
            memory_key=key,
            content=content,
            source_event_ids=[event.id],
            authority="user",
            scope=scope,
            created_by="extractor-agent",
        )

    def confirm(self, record):
        return self.service.transition_memory(
            record_id=record.id,
            target_status="confirmed",
            actor="owner-1",
            actor_authority="owner",
            reason="owner verified evidence",
            user_id=record.user_id,
        )


class TestContextCompiler(ContextCase):
    def test_confirmed_memory_compiles_ready_with_minimum_audit_fields(self):
        record = self.confirm(self.propose("杭州"))
        pack = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            access_context=self.access(),
        )
        self.assertEqual(pack.status, "ready")
        self.assertFalse(pack.abstain)
        self.assertEqual(pack.reason, "ready")
        self.assertEqual(len(pack.items), 1)
        item = pack.items[0]
        self.assertEqual(item["id"], record.id)
        self.assertEqual(item["content"], "杭州")
        self.assertEqual(item["current_status"], "confirmed")
        self.assertEqual(item["why_selected"], "confirmed_current_memory")
        self.assertTrue(item["source_locator"])
        self.assertTrue(
            all(locator.startswith("raw_events:raw_") for locator in item["source_locator"])
        )

    def test_no_evidence_is_structured_unknown(self):
        pack = self.service.compile_context(
            user_id="u1",
            memory_key="missing",
            access_context=self.access(),
        )
        self.assertEqual(pack.status, "unknown")
        self.assertTrue(pack.abstain)
        self.assertEqual(pack.reason, "no_confirmed_memory")
        self.assertEqual(pack.items, [])
        self.assertIn({"code": "no_confirmed_memory"}, pack.gaps)

    def test_conflict_keeps_signal_without_ids_or_candidate_bodies(self):
        first = self.propose("杭州")
        second = self.propose("上海")
        pack = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            access_context=self.access(),
        )
        rendered = pack.to_json()
        self.assertEqual(pack.status, "conflict")
        self.assertTrue(pack.abstain)
        self.assertEqual(
            pack.conflicts,
            [{"reason": "conflicting_current_evidence", "count": 2}],
        )
        self.assertNotIn(first.id, rendered)
        self.assertNotIn(second.id, rendered)
        self.assertNotIn("杭州", rendered)
        self.assertNotIn("上海", rendered)
        self.assertNotIn("withheld_record_ids", rendered)

    def test_owner_gate_survives_tiny_budget_and_never_returns_ready(self):
        self.propose("需要 owner 确认的方向", key="project.direction")
        pack = self.service.compile_context(
            user_id="u1",
            memory_key="project.direction",
            access_context=self.access(),
            max_chars=1,
        )
        self.assertEqual(pack.status, "budget_exceeded")
        self.assertTrue(pack.abstain)
        self.assertTrue(pack.owner_gate_required)
        self.assertIn({"code": "owner_confirmation_required"}, pack.gaps)
        self.assertEqual(pack.budget["used_chars"], len(pack.to_json()))
        self.assertGreater(pack.budget["used_chars"], 1)

    def test_character_budget_counts_final_canonical_serialization_exactly(self):
        self.confirm(self.propose("可验证的当前记忆"))
        pack = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            access_context=self.access(),
            max_chars=10_000,
        )
        self.assertEqual(pack.status, "ready")
        self.assertEqual(pack.budget["used_chars"], len(pack.to_json()))
        self.assertLessEqual(len(pack.to_json()), 10_000)
        self.assertIsNone(pack.budget["used_tokens"])

    def test_missing_and_inexact_token_counters_fail_closed(self):
        self.confirm(self.propose("不会用字符冒充 token"))
        for counter in (None, InexactCounter()):
            with self.subTest(counter=counter):
                pack = self.service.compile_context(
                    user_id="u1",
                    memory_key="profile.city",
                    access_context=self.access(),
                    max_tokens=10_000,
                    token_counter=counter,
                )
                self.assertEqual(pack.status, "budget_exceeded")
                self.assertTrue(pack.abstain)
                self.assertEqual(pack.reason, "token_counter_unavailable")
                self.assertEqual(pack.items, [])
                self.assertIsNone(pack.budget["used_tokens"])

    def test_injected_exact_counter_counts_final_json(self):
        self.confirm(self.propose("精确 token counter"))
        counter = ExactCharacterCounter()
        pack = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            access_context=self.access(),
            max_tokens=10_000,
            token_counter=counter,
        )
        self.assertEqual(pack.status, "ready")
        self.assertEqual(pack.budget["used_tokens"], counter.count_tokens(pack.to_json()))
        self.assertLessEqual(pack.budget["used_tokens"], 10_000)

    def test_broken_or_invalid_counter_return_is_rejected(self):
        self.confirm(self.propose("坏 counter 也不能放行"))
        for counter in (BrokenCounter(), BoolCounter()):
            with self.subTest(counter=type(counter).__name__):
                pack = self.service.compile_context(
                    user_id="u1",
                    memory_key="profile.city",
                    access_context=self.access(),
                    max_tokens=10_000,
                    token_counter=counter,
                )
                self.assertEqual(pack.status, "budget_exceeded")
                self.assertEqual(pack.reason, "token_counter_unavailable")
                self.assertEqual(pack.items, [])

    def test_minimum_disclosure_is_an_explicit_allowlist(self):
        record = self.confirm(self.propose("正文可见，内部推理不可见"))
        pack = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            access_context=self.access(),
        )
        item = pack.items[0]
        forbidden = {
            "source_event_ids",
            "created_by",
            "created_at",
            "updated_at",
            "confirmed_by",
            "confirmed_at",
            "state_reason",
            "confidence",
            "supersedes_record_id",
            "raw_events",
            "state_events",
        }
        self.assertTrue(forbidden.isdisjoint(item))
        self.assertNotIn("owner verified evidence", pack.to_json())
        self.assertNotIn("owner-1", pack.to_json())
        with self.assertRaises(ValueError):
            DisclosurePolicy(record_fields=frozenset({"content", "state_reason"}))

    def test_audit_requires_permission_nonempty_allowlisted_purpose(self):
        self.confirm(self.propose("审计正文"))
        no_permission = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            mode="audit",
            access_context=self.access(purpose="governance_review"),
            disclosure_policy=DisclosurePolicy(
                allowed_audit_purposes=frozenset({"governance_review"})
            ),
        )
        self.assertEqual(no_permission.status, "forbidden")
        self.assertEqual(no_permission.reason, "access_denied")

        no_purpose = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            mode="audit",
            access_context=self.access(permissions=(PERMISSION_READ, PERMISSION_AUDIT)),
            disclosure_policy=DisclosurePolicy(
                allowed_audit_purposes=frozenset({"governance_review"})
            ),
        )
        self.assertEqual(no_purpose.status, "forbidden")
        self.assertEqual(no_purpose.reason, "audit_purpose_required")

        default_closed = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            mode="audit",
            access_context=self.access(
                permissions=(PERMISSION_READ, PERMISSION_AUDIT),
                purpose="governance_review",
            ),
        )
        self.assertEqual(default_closed.status, "forbidden")
        self.assertEqual(default_closed.reason, "audit_purpose_not_allowed")

        allowed = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            mode="audit",
            access_context=self.access(
                permissions=(PERMISSION_READ, PERMISSION_AUDIT),
                purpose="governance_review",
            ),
            disclosure_policy=DisclosurePolicy(
                allowed_audit_purposes=frozenset({"governance_review"})
            ),
        )
        self.assertEqual(allowed.status, "ready")
        self.assertEqual(allowed.items[0]["why_selected"], "audit_lifecycle_record")

    def test_audit_remains_minimal_and_does_not_emit_raw_or_transition_body(self):
        record = self.confirm(self.propose("原始事件里的敏感正文"))
        audit = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            mode="audit",
            access_context=self.access(
                permissions=(PERMISSION_READ, PERMISSION_AUDIT),
                purpose="incident_response",
            ),
            disclosure_policy=DisclosurePolicy(
                record_fields=frozenset({"id", "memory_key", "content"}),
                allowed_audit_purposes=frozenset({"incident_response"}),
            ),
        )
        rendered = audit.to_json()
        self.assertEqual(audit.status, "ready")
        self.assertEqual(audit.items[0]["id"], record.id)
        self.assertNotIn("raw_events\":", rendered)
        self.assertNotIn("state_events", rendered)
        self.assertNotIn("owner-1", rendered)
        self.assertNotIn("owner verified evidence", rendered)
        self.assertGreater(audit.omitted["by_reason"]["audit_evidence"], 0)

    def test_cross_user_and_cross_scope_access_are_forbidden(self):
        self.confirm(self.propose("Apollo", key="project.name", scope={"project": "apollo"}))
        cross_user = self.service.compile_context(
            user_id="u1",
            memory_key="project.name",
            access_context=self.access(users=("u2",)),
        )
        self.assertEqual(cross_user.status, "forbidden")

        cross_scope = self.service.compile_context(
            user_id="u1",
            memory_key="project.name",
            scope={"project": "zeus"},
            access_context=self.access(scopes={"project": "apollo"}),
        )
        self.assertEqual(cross_scope.status, "forbidden")

    def test_omitted_scope_is_bound_to_access_restriction(self):
        self.confirm(self.propose("全局", key="project.name"))
        apollo = self.confirm(
            self.propose("Apollo", key="project.name", scope={"project": "apollo"})
        )
        pack = self.service.compile_context(
            user_id="u1",
            memory_key="project.name",
            access_context=self.access(scopes={"project": "apollo"}),
        )
        self.assertEqual(pack.status, "ready")
        self.assertEqual([item["id"] for item in pack.items], [apollo.id])

    def test_same_snapshot_compiles_byte_identically(self):
        self.confirm(self.propose("确定性输出"))
        kwargs = {
            "user_id": "u1",
            "memory_key": "profile.city",
            "access_context": self.access(),
            "max_chars": 10_000,
        }
        first = self.service.compile_context(**kwargs)
        second = self.service.compile_context(**kwargs)
        self.assertEqual(first.to_json(), second.to_json())

    def test_context_pack_is_deeply_immutable_after_budget_accounting(self):
        self.confirm(self.propose("不可在计数后篡改"))
        pack = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            access_context=self.access(),
            max_chars=10_000,
        )
        self.propose("待 owner 确认", key="pending.key")
        owner_pack = self.service.compile_context(
            user_id="u1",
            memory_key="pending.key",
            access_context=self.access(),
            max_chars=10_000,
        )
        self.propose("候选甲", key="conflict.key")
        self.propose("候选乙", key="conflict.key")
        conflict_pack = self.service.compile_context(
            user_id="u1",
            memory_key="conflict.key",
            access_context=self.access(),
            max_chars=10_000,
        )
        packs = (pack, owner_pack, conflict_pack)
        before = tuple(item.to_json() for item in packs)
        with self.assertRaises(AttributeError):
            pack.items.append({"content": "绕过预算"})
        with self.assertRaises(TypeError):
            pack.items[0]["content"] = "绕过预算"
        with self.assertRaises(TypeError):
            pack.budget["used_chars"] = 0

        # Reviewer reproduction: private backing objects are also sealed.  A
        # leading underscore is not the security boundary.
        mapping_backings = (
            pack.items[0]._values,
            owner_pack.gaps[0]._values,
            conflict_pack.conflicts[0]._values,
            pack.omitted._values,
            pack.omitted["by_reason"]._values,
            pack.budget._values,
        )
        for backing in mapping_backings:
            with self.assertRaises(TypeError):
                backing["tampered"] = True

        wrappers = (
            pack.items,
            pack.items[0],
            owner_pack.gaps,
            owner_pack.gaps[0],
            conflict_pack.conflicts,
            conflict_pack.conflicts[0],
            pack.omitted,
            pack.omitted["by_reason"],
            pack.budget,
        )
        for wrapper in wrappers:
            with self.assertRaises(AttributeError):
                wrapper._values = {}

        self.assertEqual(tuple(item.to_json() for item in packs), before)
        for item in packs:
            self.assertEqual(item.budget["used_chars"], len(item.to_json()))

    def test_inconsistent_current_result_never_discloses_record_content(self):
        confirmed = self.confirm(self.propose("合法 confirmed 正文"))
        fixtures = (
            (
                "unknown_with_record",
                "UNKNOWN SECRET",
                CurrentStateResult(
                    mode="current",
                    current_status="unknown",
                    abstain=True,
                    reason="no_confirmed_current_memory",
                    records=[replace(confirmed, content="UNKNOWN SECRET")],
                ),
            ),
            (
                "abstain_with_confirmed_record",
                "ABSTAIN SECRET",
                CurrentStateResult(
                    mode="current",
                    current_status="confirmed",
                    abstain=True,
                    reason="no_confirmed_current_memory",
                    records=[replace(confirmed, content="ABSTAIN SECRET")],
                ),
            ),
            (
                "candidate_record",
                "CANDIDATE SECRET",
                CurrentStateResult(
                    mode="current",
                    current_status="confirmed",
                    abstain=False,
                    reason="confirmed_current_memory",
                    records=[
                        replace(
                            confirmed,
                            status="candidate",
                            content="CANDIDATE SECRET",
                        )
                    ],
                ),
            ),
            (
                "deleted_record",
                "DELETED SECRET",
                CurrentStateResult(
                    mode="ordinary",
                    current_status="confirmed",
                    abstain=False,
                    reason="confirmed_current_memory",
                    records=[
                        replace(
                            confirmed,
                            status="deleted",
                            content="DELETED SECRET",
                        )
                    ],
                ),
            ),
            (
                "confirmed_without_records",
                "NO SECRET",
                CurrentStateResult(
                    mode="current",
                    current_status="confirmed",
                    abstain=False,
                    reason="confirmed_current_memory",
                    records=[],
                ),
            ),
            (
                "unknown_mode",
                "MODE SECRET",
                CurrentStateResult(
                    mode="untrusted-mode",
                    current_status="confirmed",
                    abstain=False,
                    reason="confirmed_current_memory",
                    records=[replace(confirmed, content="MODE SECRET")],
                ),
            ),
            (
                "invalid_audit_status",
                "AUDIT SECRET",
                CurrentStateResult(
                    mode="audit",
                    current_status="confirmed",
                    abstain=False,
                    reason="full_audit_history",
                    records=[replace(confirmed, content="AUDIT SECRET")],
                ),
            ),
        )
        compiler = ContextCompiler()
        for name, secret, result in fixtures:
            with self.subTest(name=name):
                pack = compiler.compile(result)
                self.assertEqual(pack.status, "unknown")
                self.assertTrue(pack.abstain)
                self.assertEqual(pack.reason, "invalid_governed_result")
                self.assertEqual(pack.items, [])
                self.assertNotIn(secret, pack.to_json())

    def test_inconsistent_conflict_result_keeps_signal_but_hides_record(self):
        confirmed = self.confirm(self.propose("CONFLICT SECRET"))
        malicious = CurrentStateResult(
            mode="current",
            current_status="unknown",
            abstain=True,
            reason="conflicting_current_evidence",
            records=[confirmed],
            conflicts=[confirmed.id],
            withheld_record_ids=[confirmed.id],
            owner_gate_required=True,
        )
        pack = ContextCompiler().compile(malicious)
        self.assertEqual(pack.status, "conflict")
        self.assertTrue(pack.abstain)
        self.assertEqual(pack.items, [])
        self.assertEqual(pack.conflicts[0]["count"], 1)
        self.assertNotIn("CONFLICT SECRET", pack.to_json())
        self.assertNotIn(confirmed.id, pack.to_json())

    def test_memory_items_are_atomic_under_budget(self):
        self.confirm(self.propose("甲" * 500, key="a.key"))
        self.confirm(self.propose("乙" * 500, key="b.key"))
        generous = self.service.compile_context(
            user_id="u1",
            access_context=self.access(),
            max_chars=10_000,
        )
        self.assertEqual(len(generous.items), 2)

        first_only = self.service.compile_context(
            user_id="u1",
            memory_key="a.key",
            access_context=self.access(),
            max_chars=10_000,
        )
        limited = self.service.compile_context(
            user_id="u1",
            access_context=self.access(),
            max_chars=first_only.budget["used_chars"] + 100,
        )
        self.assertEqual(limited.status, "ready")
        self.assertEqual(len(limited.items), 1)
        self.assertEqual(limited.items[0]["content"], "甲" * 500)
        self.assertEqual(limited.omitted["by_reason"]["budget"], 1)

    def test_invalid_budget_types_and_both_limits_fail_closed(self):
        self.confirm(self.propose("预算验证"))
        for invalid in (True, 1.5, "100", -1):
            with self.subTest(invalid=invalid):
                pack = self.service.compile_context(
                    user_id="u1",
                    memory_key="profile.city",
                    access_context=self.access(),
                    max_chars=invalid,
                )
                self.assertEqual(pack.status, "budget_exceeded")
                self.assertEqual(pack.reason, "invalid_budget")

        counter = ExactCharacterCounter()
        both = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            access_context=self.access(),
            max_chars=10_000,
            max_tokens=10_000,
            token_counter=counter,
        )
        self.assertEqual(both.status, "ready")
        self.assertLessEqual(both.budget["used_chars"], 10_000)
        self.assertLessEqual(both.budget["used_tokens"], 10_000)

    def test_max_records_is_a_strict_positive_integer(self):
        self.confirm(self.propose("max records 校验"))
        for invalid in (True, False, 0, -1, 1.0, "1"):
            with self.subTest(invalid=invalid):
                pack = self.service.compile_context(
                    user_id="u1",
                    memory_key="profile.city",
                    access_context=self.access(),
                    max_records=invalid,
                )
                self.assertEqual(pack.status, "budget_exceeded")
                self.assertTrue(pack.abstain)
                self.assertEqual(pack.reason, "invalid_max_records")

    def test_unauthorized_request_is_rejected_before_governed_search(self):
        with mock.patch.object(
            self.service,
            "search_governed",
            side_effect=AssertionError("unauthorized request reached retrieval"),
        ) as search:
            pack = self.service.compile_context(
                user_id="u1",
                memory_key="profile.city",
                access_context=self.access(users=("u2",)),
            )
        self.assertEqual(pack.status, "forbidden")
        search.assert_not_called()

    def test_access_context_cannot_be_self_reported_as_payload(self):
        self.confirm(self.propose("不能伪造 authority"))
        pack = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            access_context={
                "principal_id": "attacker",
                "authority": "owner",
                "permissions": [PERMISSION_READ],
                "allowed_users": ["u1"],
            },
        )
        self.assertEqual(pack.status, "forbidden")
        self.assertEqual(pack.reason, "access_denied")

    def test_wildcard_principal_cannot_read_audit_history(self):
        self.confirm(self.propose("审计不可 wildcard"))
        pack = self.service.compile_context(
            user_id="u1",
            memory_key="profile.city",
            mode="audit",
            access_context=self.access(
                users=("*",),
                permissions=(PERMISSION_READ, PERMISSION_AUDIT),
                purpose="governance_review",
            ),
            disclosure_policy=DisclosurePolicy(
                allowed_audit_purposes=frozenset({"governance_review"})
            ),
        )
        self.assertEqual(pack.status, "forbidden")

    def test_audit_purpose_rejects_control_characters_and_excess_length(self):
        for purpose in ("audit\n", "x" * 257):
            with self.subTest(purpose=purpose[:10]):
                with self.assertRaises(ValueError):
                    self.access(
                        permissions=(PERMISSION_READ, PERMISSION_AUDIT),
                        purpose=purpose,
                    )


if __name__ == "__main__":
    unittest.main()
