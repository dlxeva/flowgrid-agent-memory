"""Direct record-ID binding for governed REST transitions."""
from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aml_retriever.access import (
    PERMISSION_AUDIT,
    PERMISSION_READ,
    AccessContext,
    DisclosurePolicy,
)
from aml_retriever.facade import FlowGridMemory
from aml_retriever.rest_v1 import GovernedRestAdapter, GovernedRestConfig
from tests.test_rest_v1 import directive, make_principal


class TransitionLookupHttpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="flowgrid-transition-lookup-")
        self.db_path = str(Path(self.directory.name) / "transition.db")
        self.adapter = GovernedRestAdapter(
            GovernedRestConfig(
                db_path=self.db_path,
                principal=make_principal(scopes={}),
                auth_mode="local",
                port=0,
            )
        )
        self.thread = threading.Thread(
            target=self.adapter.serve_forever,
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.adapter.close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def request(self, path: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        encoded = json.dumps(body).encode("utf-8")
        connection = http.client.HTTPConnection(*self.adapter.address, timeout=10)
        try:
            connection.request(
                "POST",
                path,
                body=encoded,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            return response.status, payload
        finally:
            connection.close()

    def seed_candidate(
        self,
        *,
        suffix: str,
        memory_key: str,
        project: str = "alpha",
    ) -> str:
        status, added = self.request(
            "/v1/events",
            {
                "request_id": f"transition-source-{suffix}",
                "user_id": "u1",
                "session_id": f"transition-session-{suffix}",
                "scope": {"project": project},
                "messages": [
                    {
                        "role": "user",
                        "content": directive(
                            f"transition-value-{suffix}",
                            memory_key=memory_key,
                        ),
                    }
                ],
            },
        )
        self.assertEqual(status, 200, added)
        status, extracted = self.request(
            "/v1/extractions",
            {
                "user_id": "u1",
                "raw_event_ids": added["receipt"]["raw_event_ids"],
                "idempotency_key": f"transition-extract-{suffix}",
                "scope": {"project": project},
            },
        )
        self.assertEqual(status, 200, extracted)
        return extracted["receipt"]["record_ids"][0]

    def transition_body(
        self,
        *,
        record_id: str,
        memory_key: str,
        target_status: str = "confirmed",
        project: str = "alpha",
        related_record_id: str | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "user_id": "u1",
            "record_id": record_id,
            "memory_key": memory_key,
            "target_status": target_status,
            "reason": "owner verified exact transition target",
            "scope": {"project": project},
        }
        if related_record_id is not None:
            body["related_record_id"] = related_record_id
        return body

    def record_status(self, record_id: str) -> str:
        with sqlite3.connect(self.db_path) as con:
            row = con.execute(
                "SELECT status FROM memory_records WHERE id=?",
                (record_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return row[0]


class TestDirectRestTransitionLookup(TransitionLookupHttpCase):
    def test_transition_does_not_call_governed_audit_search(self) -> None:
        record_id = self.seed_candidate(
            suffix="no-audit-scan",
            memory_key="project.decision",
        )
        memory = self.adapter._memory
        self.assertIsNotNone(memory)
        with mock.patch.object(
            memory,
            "query_memory",
            side_effect=AssertionError("transition reached governed audit search"),
        ) as audit_search:
            status, result = self.request(
                "/v1/memories/transition",
                self.transition_body(
                    record_id=record_id,
                    memory_key="project.decision",
                ),
            )
        self.assertEqual(status, 200, result)
        audit_search.assert_not_called()
        self.assertEqual(
            result,
            {
                "record": {
                    "record_id": record_id,
                    "current_status": "confirmed",
                }
            },
        )

    def test_exact_key_scope_and_related_slot_are_required(self) -> None:
        target = self.seed_candidate(
            suffix="target-binding",
            memory_key="project.decision",
        )
        unrelated = self.seed_candidate(
            suffix="unrelated-binding",
            memory_key="project.risk",
        )
        denied_bodies = (
            self.transition_body(
                record_id=target,
                memory_key="private.wrong.key",
            ),
            self.transition_body(
                record_id=target,
                memory_key="project.decision",
                project="beta",
            ),
            self.transition_body(
                record_id=target,
                memory_key="project.decision",
                related_record_id=target,
            ),
            self.transition_body(
                record_id=target,
                memory_key="project.decision",
                related_record_id=unrelated,
            ),
        )
        for body in denied_bodies:
            with self.subTest(body=body):
                status, error = self.request("/v1/memories/transition", body)
                self.assertEqual(status, 403, error)
                self.assertEqual(error["error"]["code"], "forbidden")
                rendered = json.dumps(error)
                self.assertNotIn(target, rendered)
                self.assertNotIn(unrelated, rendered)
                self.assertNotIn("private.wrong.key", rendered)
                self.assertEqual(self.record_status(target), "candidate")

        status, result = self.request(
            "/v1/memories/transition",
            self.transition_body(
                record_id=target,
                memory_key="project.decision",
            ),
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(self.record_status(target), "confirmed")

    def test_target_after_the_old_ten_thousand_record_window_is_reachable(self) -> None:
        memory_key = "project.large-review-window"
        target = self.seed_candidate(
            suffix="large-window-target",
            memory_key=memory_key,
        )
        old_instant = "2000-01-01T00:00:00+00:00"
        noise_count = 10_001
        with sqlite3.connect(self.db_path) as con:
            con.executemany(
                """INSERT INTO memory_records(
                    id,user_id,memory_key,memory_type,subject,content,status,
                    source_event_ids,observed_at,valid_from,valid_until,authority,
                    scope_json,confidence,created_by,created_at,updated_at,
                    confirmed_by,confirmed_at,supersedes_record_id,state_reason
                )
                SELECT ?,user_id,memory_key,memory_type,subject,?,status,
                    source_event_ids,observed_at,valid_from,valid_until,authority,
                    scope_json,confidence,created_by,?,?,confirmed_by,confirmed_at,
                    supersedes_record_id,state_reason
                FROM memory_records WHERE id=?""",
                (
                    (
                        f"mem_noise_{index:05d}",
                        f"noise-{index}",
                        old_instant,
                        old_instant,
                        target,
                    )
                    for index in range(noise_count)
                ),
            )
            con.executemany(
                """INSERT INTO memory_state_events(
                    id,record_id,user_id,from_status,to_status,actor,
                    actor_authority,reason,transitioned_at,related_record_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        f"tr_noise_{index:05d}",
                        f"mem_noise_{index:05d}",
                        "u1",
                        "",
                        "candidate",
                        "scale-fixture",
                        "agent",
                        "scale fixture",
                        old_instant,
                        None,
                    )
                    for index in range(noise_count)
                ),
            )
            con.commit()

        status, result = self.request(
            "/v1/memories/transition",
            self.transition_body(
                record_id=target,
                memory_key=memory_key,
                target_status="rejected",
            ),
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["record"]["current_status"], "rejected")
        self.assertEqual(self.record_status(target), "rejected")


class TestFacadeTransitionAuthorization(unittest.TestCase):
    def test_denial_happens_before_direct_database_lookup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="flowgrid-transition-facade-") as directory:
            path = str(Path(directory) / "facade.db")
            with FlowGridMemory(db_path=path) as memory:
                denied = AccessContext(
                    principal_id="auditor",
                    authority="owner",
                    scopes={"project": "alpha"},
                    permissions=frozenset({PERMISSION_READ, PERMISSION_AUDIT}),
                    purpose="transition review",
                    allowed_users=frozenset({"someone-else"}),
                )
                policy = DisclosurePolicy(
                    allowed_audit_purposes=frozenset({"transition review"})
                )
                service = memory._active()
                with mock.patch.object(
                    service,
                    "transition_target_matches",
                    side_effect=AssertionError("denied request reached database lookup"),
                ) as lookup:
                    allowed = memory.authorize_transition_target(
                        user_id="u1",
                        record_id="mem_private",
                        memory_key="project.decision",
                        access_context=denied,
                        scope={"project": "alpha"},
                        disclosure_policy=policy,
                    )
                self.assertFalse(allowed)
                lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
