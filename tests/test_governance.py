"""Governed current-state invariants (FLG philosophy gate)."""
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from aml_retriever.api import MemoryService
from aml_retriever.config import RetrieverConfig
from aml_retriever.governance import GovernanceConflict, GovernanceError
from aml_retriever.retriever import RetrieverDB
from tests.legacy_fixture import create_legacy_database



class GovernanceCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.service = MemoryService(RetrieverConfig(db_path=self.path))
        self.serial = 0

    def tearDown(self):
        if self.service is not None:
            self.service.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def reset_to_legacy_db(self, *, content="旧库中的原始消息"):
        self.service.close()
        self.service = None
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass
        # A messages-only lookalike is not a valid predecessor. This fixture
        # reproduces the accepted schema, indexes, and FTS contract.
        create_legacy_database(self.path, content=content)

    def add_event(self, content, *, role="user", user_id="u1", session_id="s1"):
        self.serial += 1
        result = self.service.add(
            request_id=f"req-{self.serial}",
            user_id=user_id,
            session_id=session_id,
            messages=[{"role": role, "content": content, "timestamp": 1_700_000_000_000 + self.serial}],
        )
        message_id = result.message_ids[0]
        events = self.service.list_raw_events(user_id)
        return next(event for event in events if event.source_message_id == message_id)

    def propose(self, event, content, *, key="profile.city", **overrides):
        data = {
            "user_id": event.user_id,
            "memory_key": key,
            "content": content,
            "source_event_ids": [event.id],
            "authority": event.authority,
            "created_by": "agent",
        }
        data.update(overrides)
        return self.service.propose_memory(**data)

    def confirm(self, record, *, actor="owner-1"):
        return self.service.transition_memory(
            record_id=record.id,
            target_status="confirmed",
            actor=actor,
            actor_authority="owner",
            reason="owner verified source evidence",
            user_id=record.user_id,
        )


class TestRawEventBoundary(GovernanceCase):
    def test_raw_event_maps_to_message_without_copying_body(self):
        event = self.add_event("  原始内容必须逐字保留 Mixed Case  ")
        self.assertEqual(event.content, "  原始内容必须逐字保留 Mixed Case  ")
        columns = {row["name"] for row in self.service.db.query("PRAGMA table_info(raw_events)")}
        self.assertNotIn("content", columns, "RawEvent 应定位 source message，不复制正文")
        self.assertNotIn("role", columns)
        self.assertEqual(event.source_locator, f"messages:{event.source_message_id}")

    def test_raw_event_and_source_message_are_immutable(self):
        event = self.add_event("不可改写的证据")
        with self.assertRaises(sqlite3.DatabaseError):
            self.service.db._write(
                lambda con: con.execute(
                    "UPDATE raw_events SET authority='agent' WHERE id=?", (event.id,)
                )
            )

    def test_derived_payload_is_immutable_but_projection_fields_can_update(self):
        event = self.add_event("不可变派生证据")
        record = self.propose(event, "不可变派生内容", key="immutable.sample")
        with self.assertRaises(sqlite3.DatabaseError):
            self.service.db._write(
                lambda con: con.execute(
                    "UPDATE memory_records SET content='篡改' WHERE id=?", (record.id,)
                )
            )
        # status is explicitly a projection cache; the append-only state stream
        # remains authoritative and is tested separately.
        self.service.db._write(
            lambda con: con.execute(
                "UPDATE memory_records SET status='unknown',updated_at=updated_at WHERE id=?",
                (record.id,),
            )
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.service.db._write(
                lambda con: con.execute(
                    "UPDATE messages SET content='被篡改' WHERE id=?", (event.source_message_id,)
                )
            )

    def test_reopen_is_idempotent_non_destructive_migration(self):
        event = self.add_event("迁移后仍可追溯")
        version = self.service.db.query(
            "SELECT value FROM governance_meta WHERE key='schema_version'"
        )[0]["value"]
        self.assertEqual(version, "1")
        self.service.close()
        with mock.patch(
            "aml_retriever.governance.backfill_raw_events",
            side_effect=AssertionError("versioned migration must not rescan messages"),
        ):
            self.service = MemoryService(RetrieverConfig(db_path=self.path))
        events = self.service.list_raw_events("u1")
        self.assertEqual([item.id for item in events], [event.id])
        self.assertEqual(events[0].content, "迁移后仍可追溯")

    def test_legacy_database_is_backfilled_once(self):
        self.reset_to_legacy_db(content="旧库一次性迁移")
        self.service = MemoryService(RetrieverConfig(db_path=self.path))
        events = self.service.list_raw_events("u1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].content, "旧库一次性迁移")
        self.assertEqual(
            self.service.db.query(
                "SELECT value FROM governance_meta WHERE key='schema_version'"
            )[0]["value"],
            "1",
        )

    def test_two_instances_can_initialize_same_legacy_database(self):
        self.reset_to_legacy_db(content="并发迁移只做一次")
        barrier = threading.Barrier(3)
        errors = []
        created = []

        def start_instance():
            try:
                barrier.wait(timeout=5)
                db = RetrieverDB(RetrieverConfig(db_path=self.path))
                created.append(db)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=start_instance) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)
        try:
            self.assertEqual(errors, [])
            self.assertEqual(len(created), 2)
            self.assertEqual(created[0].query("SELECT COUNT(*) FROM raw_events")[0][0], 1)
        finally:
            for db in created:
                db.close()
        self.service = MemoryService(RetrieverConfig(db_path=self.path))


class TestAuthorityAndUnknown(GovernanceCase):
    def test_memory_content_must_be_non_empty(self):
        event = self.add_event("有来源")
        for bad in ("", "   "):
            with self.assertRaises(GovernanceError):
                self.propose(event, bad, key="empty.content")

    def test_transition_requires_matching_user_id(self):
        event = self.add_event("用户隔离的状态变更")
        record = self.propose(event, "隔离", key="isolation.transition")
        with self.assertRaises(TypeError):
            self.service.transition_memory(
                record_id=record.id,
                target_status="rejected",
                actor="owner-1",
                actor_authority="owner",
                reason="missing user scope",
            )
        with self.assertRaises(GovernanceError):
            self.service.transition_memory(
                record_id=record.id,
                target_status="rejected",
                actor="owner-1",
                actor_authority="owner",
                reason="wrong user scope",
                user_id="other-user",
            )

    def test_related_record_must_share_user_and_slot_when_slot_related(self):
        event = self.add_event("主记录")
        record = self.propose(event, "主记录", key="related.main")
        other_event = self.add_event("其他用户记录", user_id="u2")
        other_user = self.propose(other_event, "其他用户记录", key="related.main")
        with self.assertRaises(GovernanceError):
            self.service.transition_memory(
                record_id=record.id,
                target_status="inferred",
                actor="agent-1",
                actor_authority="agent",
                reason="cross-user relation is invalid",
                user_id="u1",
                related_record_id=other_user.id,
            )

        unrelated = self.propose(event, "不同槽", key="related.other")
        with self.assertRaises(GovernanceError):
            self.service.transition_memory(
                record_id=record.id,
                target_status="confirmed",
                actor="owner-1",
                actor_authority="owner",
                reason="cross-slot relation is invalid",
                user_id="u1",
                related_record_id=unrelated.id,
            )

    def test_governance_terminal_states_require_governance_authority(self):
        event = self.add_event("待治理记录")
        record = self.propose(event, "待治理", key="authority.terminal")
        for target, authority in (("rejected", "agent"), ("deleted", "system")):
            with self.assertRaises(GovernanceError):
                self.service.transition_memory(
                    record_id=record.id,
                    target_status=target,
                    actor=f"{authority}-actor",
                    actor_authority=authority,
                    reason="unauthorized terminal transition",
                    user_id="u1",
                )
        self.confirm(record)
        with self.assertRaises(GovernanceError):
            self.service.transition_memory(
                record_id=record.id,
                target_status="deleted",
                actor="external-actor",
                actor_authority="external",
                reason="external cannot delete confirmed truth",
                user_id="u1",
            )

    def test_agent_proposal_never_silently_becomes_current(self):
        event = self.add_event("我住在杭州")
        candidate = self.propose(event, "杭州")
        current = self.service.search_governed(user_id="u1", memory_key="profile.city")
        self.assertEqual(current.current_status, "unknown")
        self.assertTrue(current.abstain)
        self.assertTrue(current.owner_gate_required)
        self.assertEqual(current.records, [])

        with self.assertRaises(GovernanceError):
            self.service.propose_memory(
                user_id="u1",
                memory_key="profile.country",
                content="中国",
                source_event_ids=[event.id],
                status="confirmed",
                authority="user",
                created_by="agent",
            )
        with self.assertRaises(GovernanceError):
            self.service.transition_memory(
                record_id=candidate.id,
                target_status="confirmed",
                actor="extractor-agent",
                actor_authority="agent",
                reason="model thinks this is true",
                user_id="u1",
            )

        self.confirm(candidate)
        current = self.service.search_governed(user_id="u1", memory_key="profile.city")
        self.assertEqual(current.current_status, "confirmed")
        self.assertFalse(current.abstain)
        self.assertEqual([item.content for item in current.records], ["杭州"])

    def test_no_evidence_and_competing_proposals_are_structured_unknown(self):
        empty = self.service.search_governed(user_id="u1", memory_key="missing")
        self.assertEqual(empty.to_dict()["current_status"], "unknown")
        self.assertTrue(empty.to_dict()["abstain"])
        self.assertEqual(empty.reason, "no_matching_memory")

        event = self.add_event("城市信息可能是杭州，也可能是上海")
        first = self.propose(event, "杭州")
        second = self.propose(event, "上海")
        conflict = self.service.search_governed(user_id="u1", memory_key="profile.city")
        self.assertEqual(conflict.current_status, "unknown")
        self.assertTrue(conflict.abstain)
        self.assertEqual(conflict.reason, "conflicting_current_evidence")
        self.assertEqual(set(conflict.conflicts), {first.id, second.id})

    def test_persisted_unknown_is_a_boundary_not_recalled_content(self):
        record = self.service.propose_memory(
            user_id="u1",
            memory_key="profile.city",
            content="模型猜测可能在杭州",
            source_event_ids=[],
            status="unknown",
            authority="agent",
            created_by="agent",
            state_reason="insufficient evidence",
        )
        current = self.service.search_governed(user_id="u1", memory_key="profile.city")
        self.assertTrue(current.abstain)
        self.assertEqual(current.records, [])
        self.assertIn(record.id, current.withheld_record_ids)

    def test_query_percent_and_underscore_are_literal(self):
        percent_event = self.add_event("完成度标记是 100%_fixed")
        percent = self.propose(percent_event, "100%_fixed", key="literal.percent")
        self.confirm(percent)
        other_event = self.add_event("完全无关的普通内容")
        other = self.propose(other_event, "普通内容", key="literal.other")
        self.confirm(other)
        result = self.service.search_governed(user_id="u1", query="%_")
        self.assertEqual([record.id for record in result.records], [percent.id])


class TestCurrentStateLifecycle(GovernanceCase):
    def test_non_overlapping_confirmed_intervals_coexist_and_resolve_by_as_of(self):
        base = datetime.now(timezone.utc)
        tomorrow = (base + timedelta(days=1)).isoformat()
        day_after = (base + timedelta(days=2)).isoformat()
        later = (base + timedelta(days=4)).isoformat()

        old_event = self.add_event("本阶段负责人是小王")
        old = self.propose(
            old_event,
            "小王",
            key="validity.owner",
            valid_from=(base - timedelta(days=1)).isoformat(),
            valid_until=tomorrow,
        )
        self.confirm(old)
        new_event = self.add_event("后续阶段负责人是小李")
        new = self.propose(
            new_event,
            "小李",
            key="validity.owner",
            valid_from=day_after,
            valid_until=later,
        )
        self.confirm(new)  # no supersedes relation: intervals do not overlap

        current = self.service.search_governed(user_id="u1", memory_key="validity.owner")
        self.assertEqual([record.id for record in current.records], [old.id])
        future = self.service.search_governed(
            user_id="u1",
            memory_key="validity.owner",
            as_of=(base + timedelta(days=3)).isoformat(),
        )
        self.assertEqual([record.id for record in future.records], [new.id])

        # Half-open endpoints: [a, tomorrow) and [tomorrow, b) are adjacent,
        # not overlapping, so both may be confirmed in the same exact slot.
        adjacent_old_event = self.add_event("相邻阶段 A")
        adjacent_old = self.propose(
            adjacent_old_event,
            "A",
            key="validity.adjacent",
            valid_from=(base - timedelta(days=1)).isoformat(),
            valid_until=tomorrow,
        )
        self.confirm(adjacent_old)
        adjacent_new_event = self.add_event("相邻阶段 B")
        adjacent_new = self.propose(
            adjacent_new_event,
            "B",
            key="validity.adjacent",
            valid_from=tomorrow,
            valid_until=day_after,
        )
        self.confirm(adjacent_new)
        at_boundary = self.service.search_governed(
            user_id="u1", memory_key="validity.adjacent", as_of=tomorrow
        )
        self.assertEqual([record.id for record in at_boundary.records], [adjacent_new.id])

    def test_overlapping_future_confirmed_intervals_require_supersession(self):
        base = datetime.now(timezone.utc)
        tomorrow = (base + timedelta(days=1)).isoformat()
        first_event = self.add_event("未来计划版本一")
        first = self.propose(
            first_event,
            "版本一",
            key="validity.future_plan",
            valid_from=tomorrow,
            valid_until=(base + timedelta(days=4)).isoformat(),
        )
        self.confirm(first)
        second_event = self.add_event("未来计划版本二")
        second = self.propose(
            second_event,
            "版本二",
            key="validity.future_plan",
            valid_from=tomorrow,
            valid_until=(base + timedelta(days=3)).isoformat(),
        )
        with self.assertRaises(GovernanceConflict):
            self.confirm(second)

    def test_future_scheduled_supersession_fails_closed_without_hiding_current(self):
        base = datetime.now(timezone.utc)
        old_event = self.add_event("今天仍生效的负责人是小王")
        old = self.propose(
            old_event,
            "小王",
            key="validity.scheduled_owner",
            valid_from=(base - timedelta(days=1)).isoformat(),
            valid_until=(base + timedelta(days=10)).isoformat(),
        )
        self.confirm(old)
        future_event = self.add_event("明天计划换成小李")
        future = self.propose(
            future_event,
            "小李",
            key="validity.scheduled_owner",
            valid_from=(base + timedelta(days=1)).isoformat(),
            valid_until=(base + timedelta(days=5)).isoformat(),
            supersedes_record_id=old.id,
        )
        with self.assertRaises(GovernanceConflict) as ctx:
            self.confirm(future)
        self.assertIn("replacement must be currently valid", str(ctx.exception))

        current = self.service.search_governed(
            user_id="u1", memory_key="validity.scheduled_owner"
        )
        self.assertEqual([record.id for record in current.records], [old.id])
        self.assertEqual(current.records[0].status, "confirmed")
        audit = self.service.search_governed(
            user_id="u1", memory_key="validity.scheduled_owner", mode="audit"
        )
        self.assertEqual(
            {record.id: record.status for record in audit.records},
            {old.id: "confirmed", future.id: "candidate"},
        )
        self.assertFalse(any(
            event.record_id == old.id and event.to_status == "superseded"
            for event in audit.state_events
        ))

    def test_expired_supersession_fails_closed_without_hiding_current(self):
        base = datetime.now(timezone.utc)
        old_event = self.add_event("今天有效的方案是 A")
        old = self.propose(
            old_event,
            "A",
            key="validity.expired_replacement",
            valid_from=(base - timedelta(days=10)).isoformat(),
            valid_until=(base + timedelta(days=10)).isoformat(),
        )
        self.confirm(old)
        expired_event = self.add_event("已经过期的替代方案 B")
        expired = self.propose(
            expired_event,
            "B",
            key="validity.expired_replacement",
            valid_from=(base - timedelta(days=5)).isoformat(),
            valid_until=(base - timedelta(days=1)).isoformat(),
            supersedes_record_id=old.id,
        )
        with self.assertRaises(GovernanceConflict) as ctx:
            self.confirm(expired)
        self.assertIn("expired supersession is unsupported", str(ctx.exception))

        current = self.service.search_governed(
            user_id="u1", memory_key="validity.expired_replacement"
        )
        self.assertEqual([record.id for record in current.records], [old.id])
        self.assertEqual(current.records[0].status, "confirmed")
        audit = self.service.search_governed(
            user_id="u1", memory_key="validity.expired_replacement", mode="audit"
        )
        self.assertEqual(
            {record.id: record.status for record in audit.records},
            {old.id: "confirmed", expired.id: "candidate"},
        )
        self.assertFalse(any(
            event.record_id == old.id and event.to_status == "superseded"
            for event in audit.state_events
        ))

    def test_slot_identity_and_scope_specificity_prevent_cross_project_override(self):
        global_event = self.add_event("全局负责人是小王")
        global_record = self.propose(global_event, "小王", key="project.owner")
        self.confirm(global_record)

        project_event = self.add_event("Apollo 项目负责人是小李")
        with self.assertRaises(GovernanceError):
            self.propose(
                project_event,
                "小李",
                key="project.owner",
                scope={"project": "apollo"},
                supersedes_record_id=global_record.id,
            )
        apollo = self.propose(
            project_event,
            "小李",
            key="project.owner",
            scope={"project": "apollo"},
        )
        self.confirm(apollo)

        zeus_event = self.add_event("Zeus 项目负责人是小赵")
        zeus = self.propose(
            zeus_event,
            "小赵",
            key="project.owner",
            scope={"project": "zeus"},
        )
        self.confirm(zeus)

        global_result = self.service.search_governed(user_id="u1", memory_key="project.owner")
        self.assertEqual([record.id for record in global_result.records], [global_record.id])
        apollo_result = self.service.search_governed(
            user_id="u1", memory_key="project.owner", scope={"project": "apollo"}
        )
        self.assertEqual([record.id for record in apollo_result.records], [apollo.id])
        self.assertIn(global_record.id, apollo_result.withheld_record_ids)
        zeus_result = self.service.search_governed(
            user_id="u1", memory_key="project.owner", scope={"project": "zeus"}
        )
        self.assertEqual([record.id for record in zeus_result.records], [zeus.id])

    def test_as_of_resolves_historical_state_from_state_events(self):
        old_event = self.add_event("当前城市是北京")
        old = self.propose(old_event, "北京", key="history.city")
        old = self.confirm(old)
        historical_cutoff = old.confirmed_at

        new_event = self.add_event("当前城市更新为上海")
        new = self.propose(
            new_event,
            "上海",
            key="history.city",
            supersedes_record_id=old.id,
        )
        new = self.service.transition_memory(
            record_id=new.id,
            target_status="confirmed",
            actor="future-owner",
            actor_authority="owner",
            reason="future supersession reason",
            user_id="u1",
        )

        historical = self.service.search_governed(
            user_id="u1", memory_key="history.city", as_of=historical_cutoff
        )
        self.assertEqual([record.id for record in historical.records], [old.id])
        self.assertEqual(historical.records[0].status, "confirmed")
        self.assertEqual(historical.records[0].state_reason, "owner verified source evidence")
        self.assertEqual(historical.records[0].updated_at, historical_cutoff)
        self.assertEqual(historical.records[0].confirmed_by, "owner-1")
        self.assertEqual(historical.records[0].confirmed_at, historical_cutoff)
        self.assertNotEqual(historical.records[0].updated_at, new.updated_at)
        self.assertNotEqual(historical.records[0].state_reason, "future supersession reason")
        current = self.service.search_governed(user_id="u1", memory_key="history.city")
        self.assertEqual([record.id for record in current.records], [new.id])
        self.assertEqual(current.records[0].status, "confirmed")

        audit_then = self.service.search_governed(
            user_id="u1", memory_key="history.city", mode="audit", as_of=historical_cutoff
        )
        self.assertEqual([record.id for record in audit_then.records], [old.id])
        self.assertEqual(audit_then.records[0].state_reason, "owner verified source evidence")
        self.assertEqual(audit_then.records[0].updated_at, historical_cutoff)
        self.assertEqual(audit_then.records[0].confirmed_by, "owner-1")
        self.assertEqual(audit_then.records[0].confirmed_at, historical_cutoff)
        self.assertTrue(all(event.transitioned_at <= historical_cutoff for event in audit_then.state_events))

    def test_as_of_before_confirmation_has_no_future_confirmation_projection(self):
        event = self.add_event("仍待确认的事实")
        candidate = self.propose(event, "候选值", key="history.pending")
        candidate_cutoff = candidate.created_at
        self.confirm(candidate)
        audit_then = self.service.search_governed(
            user_id="u1",
            memory_key="history.pending",
            mode="audit",
            as_of=candidate_cutoff,
        )
        self.assertEqual(audit_then.records[0].status, "candidate")
        self.assertEqual(audit_then.records[0].state_reason, "record_created")
        self.assertEqual(audit_then.records[0].updated_at, candidate_cutoff)
        self.assertIsNone(audit_then.records[0].confirmed_by)
        self.assertIsNone(audit_then.records[0].confirmed_at)

    def test_time_inputs_are_timezone_aware_and_canonicalized_to_utc(self):
        event = self.add_event("带有效期的记忆")
        plus_eight = timezone(timedelta(hours=8))
        now = datetime.now(timezone.utc)
        valid_from = (now - timedelta(hours=1)).astimezone(plus_eight).isoformat()
        valid_until = (now + timedelta(hours=1)).astimezone(plus_eight).isoformat()
        record = self.propose(
            event,
            "有效内容",
            key="time.offset",
            valid_from=valid_from,
            valid_until=valid_until,
        )
        record = self.confirm(record)
        self.assertTrue(record.valid_from.endswith("+00:00"))
        self.assertTrue(record.valid_until.endswith("+00:00"))

        cutoff_utc = record.confirmed_at
        cutoff_plus_eight = (
            datetime.fromisoformat(cutoff_utc).astimezone(plus_eight).isoformat()
        )
        utc_result = self.service.search_governed(
            user_id="u1", memory_key="time.offset", as_of=cutoff_utc
        )
        offset_result = self.service.search_governed(
            user_id="u1", memory_key="time.offset", as_of=cutoff_plus_eight
        )
        self.assertEqual([item.id for item in utc_result.records], [record.id])
        self.assertEqual([item.id for item in offset_result.records], [record.id])

        with self.assertRaises(GovernanceError):
            self.propose(
                event,
                "无时区",
                key="time.naive",
                valid_from="2026-08-24T12:00:00",
            )
        with self.assertRaises(GovernanceError):
            self.service.search_governed(
                user_id="u1", memory_key="time.offset", as_of="2026-08-24T12:00:00"
            )

    def test_max_records_must_be_positive(self):
        with self.assertRaises(GovernanceError):
            self.service.search_governed(user_id="u1", max_records=0)

    def test_confirmed_supersession_hides_old_but_audit_preserves_chain(self):
        old_event = self.add_event("我目前住在北京")
        old = self.propose(old_event, "北京")
        self.confirm(old)

        new_event = self.add_event("我已经搬到上海，不再住北京")
        new = self.propose(
            new_event,
            "上海",
            supersedes_record_id=old.id,
            state_reason="candidate update",
        )
        before = self.service.search_governed(user_id="u1", memory_key="profile.city")
        self.assertEqual([item.id for item in before.records], [old.id])
        self.assertTrue(before.owner_gate_required)
        self.confirm(new)

        current = self.service.search_governed(user_id="u1", memory_key="profile.city")
        self.assertEqual([item.id for item in current.records], [new.id])
        self.assertNotIn("北京", [item.content for item in current.records])
        self.assertIn(old.id, current.withheld_record_ids)

        audit = self.service.search_governed(
            user_id="u1", memory_key="profile.city", mode="audit"
        )
        self.assertEqual({item.status for item in audit.records}, {"confirmed", "superseded"})
        self.assertEqual({item.content for item in audit.raw_events}, {
            "我目前住在北京", "我已经搬到上海，不再住北京"
        })
        self.assertTrue(any(
            item.record_id == old.id and item.to_status == "superseded"
            for item in audit.state_events
        ))

    def test_terminal_states_never_leak_in_current_mode(self):
        event = self.add_event("旧方向是做桌面客户端")
        rejected = self.propose(event, "做桌面客户端", key="product.direction")
        self.service.transition_memory(
            record_id=rejected.id,
            target_status="rejected",
            actor="owner-1",
            actor_authority="owner",
            reason="owner rejected this direction",
            user_id="u1",
        )
        current = self.service.search_governed(
            user_id="u1", memory_key="product.direction", query="桌面"
        )
        self.assertEqual(current.records, [])
        self.assertTrue(current.abstain)

        fact_event = self.add_event("临时门禁码是 1234")
        deleted = self.propose(fact_event, "1234", key="access.code")
        self.confirm(deleted)
        self.service.transition_memory(
            record_id=deleted.id,
            target_status="deleted",
            actor="owner-1",
            actor_authority="owner",
            reason="tombstone the expired secret",
            user_id="u1",
        )
        deleted_current = self.service.search_governed(user_id="u1", memory_key="access.code")
        self.assertEqual(deleted_current.records, [])
        self.assertTrue(deleted_current.abstain)

    def test_append_only_state_events_are_authoritative_over_projection_cache(self):
        event = self.add_event("代号是北极星")
        record = self.propose(event, "北极星", key="project.codename")
        self.confirm(record)
        # Simulate a stale/corrupt projection cache. Resolver must read the
        # append-only state stream, not trust memory_records.status alone.
        self.service.db._write(
            lambda con: con.execute(
                "UPDATE memory_records SET status='deleted' WHERE id=?", (record.id,)
            )
        )
        current = self.service.search_governed(user_id="u1", memory_key="project.codename")
        self.assertEqual(current.current_status, "confirmed")
        self.assertEqual([item.id for item in current.records], [record.id])
        self.assertEqual(current.records[0].status, "confirmed")

    def test_competing_confirmation_requires_explicit_supersession(self):
        first_event = self.add_event("负责人是小王")
        first = self.propose(first_event, "小王", key="project.owner")
        self.confirm(first)
        second_event = self.add_event("也有人说负责人是小李")
        second = self.propose(second_event, "小李", key="project.owner")
        with self.assertRaises(GovernanceConflict):
            self.confirm(second)
        current = self.service.search_governed(user_id="u1", memory_key="project.owner")
        self.assertEqual([item.content for item in current.records], ["小王"])

    def test_two_db_instances_cannot_confirm_same_slot_concurrently(self):
        first_event = self.add_event("候选负责人一")
        first = self.propose(first_event, "小王", key="concurrent.owner")
        second_event = self.add_event("候选负责人二")
        second = self.propose(second_event, "小李", key="concurrent.owner")
        other_db = RetrieverDB(RetrieverConfig(db_path=self.path))
        barrier = threading.Barrier(3)
        successes = []
        errors = []

        def confirm_with(db, record):
            try:
                barrier.wait(timeout=5)
                successes.append(
                    db.transition_memory(
                        record_id=record.id,
                        target_status="confirmed",
                        actor="owner-1",
                        actor_authority="owner",
                        reason="concurrent owner confirmation",
                        user_id="u1",
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [
            threading.Thread(target=confirm_with, args=(self.service.db, first)),
            threading.Thread(target=confirm_with, args=(other_db, second)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=15)
        other_db.close()

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], GovernanceConflict)
        current = self.service.search_governed(user_id="u1", memory_key="concurrent.owner")
        self.assertEqual(len(current.records), 1)


class TestPreferenceAndPrivacy(GovernanceCase):
    def test_preference_requires_explicit_subject_and_direct_user_source(self):
        user_event = self.add_event("我偏好深色模式", role="user")
        with self.assertRaises(GovernanceError):
            self.propose(user_event, "深色模式", key="preference.theme", memory_type="preference")

        direct = self.propose(
            user_event,
            "深色模式",
            key="preference.theme",
            memory_type="preference",
            subject="u1",
            scope={"user": "u1", "project": "app"},
            authority="user",
        )
        self.confirm(direct)
        current = self.service.search_governed(
            user_id="u1", memory_key="preference.theme", scope={"project": "app"}
        )
        self.assertEqual([item.content for item in current.records], ["深色模式"])
        self.assertEqual(current.records[0].subject, "u1")
        self.assertTrue(current.records[0].observed_at)
        self.assertEqual(current.records[0].authority, "user")

        assistant_event = self.add_event("你应该喜欢浅色模式", role="assistant")
        suggested = self.propose(
            assistant_event,
            "浅色模式",
            key="preference.suggested_theme",
            memory_type="preference",
            subject="u1",
            authority="agent",
        )
        with self.assertRaises(GovernanceError):
            self.confirm(suggested)

        third_person = self.propose(
            user_event,
            "深色模式",
            key="preference.third_person",
            memory_type="preference",
            subject="somebody-else",
            authority="user",
        )
        with self.assertRaises(GovernanceError):
            self.confirm(third_person)

    def test_tombstone_preserves_audit_but_privacy_erase_removes_everything(self):
        event = self.add_event("需要被删除的个人记忆")
        record = self.propose(event, "个人记忆", key="privacy.sample")
        self.confirm(record)
        self.service.transition_memory(
            record_id=record.id,
            target_status="deleted",
            actor="owner-1",
            actor_authority="owner",
            reason="derived-memory tombstone",
            user_id="u1",
        )
        audit = self.service.search_governed(user_id="u1", memory_key="privacy.sample", mode="audit")
        self.assertEqual(audit.records[0].status, "deleted")
        self.assertEqual(audit.raw_events[0].content, "需要被删除的个人记忆")

        report = self.service.delete_user("u1")
        self.assertEqual(report["deleted_raw_events"], 1)
        self.assertEqual(report["deleted_memory_records"], 1)
        for table in ("messages", "raw_events", "memory_records", "memory_state_events"):
            self.assertEqual(
                self.service.db.query(f"SELECT COUNT(*) FROM {table} WHERE user_id='u1'")[0][0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
