from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime.team import MessageStore, TeamCoordinator


class TeamCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.logs = []
        self.db_path = self.root / ".state" / "team.db"
        self.mailboxes = self.root / ".mailboxes"
        self.coordinator = TeamCoordinator(
            mailbox_dir=self.mailboxes,
            message_db_path=self.db_path,
            terminal_print=self.logs.append,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_message_bus_round_trip(self):
        sent = self.coordinator.bus.send("worker", "lead", "hello")
        msgs = self.coordinator.bus.read_inbox("lead")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["from"], "worker")
        self.assertEqual(msgs[0]["to"], "lead")
        self.assertEqual(msgs[0]["content"], "hello")
        self.assertEqual(msgs[0]["type"], "message")
        self.assertEqual(set(sent), {"from", "to", "content", "type", "ts", "metadata"})
        self.assertTrue(any("worker → lead" in line for line in self.logs))

    def test_unread_bus_message_survives_coordinator_restart(self):
        self.coordinator.bus.send("worker", "lead", "after restart")
        restarted = TeamCoordinator(
            mailbox_dir=self.mailboxes,
            message_db_path=self.db_path,
            terminal_print=lambda _: None,
        )
        msgs = restarted.bus.read_inbox("lead")
        self.assertEqual([msg["content"] for msg in msgs], ["after restart"])
        self.assertEqual(self.coordinator.bus.read_inbox("lead"), [])

    def test_submit_plan_creates_pending_request_and_lead_message(self):
        result = self.coordinator.submit_plan(
            "proof_worker",
            "# Proof Plan: Test",
        )
        request_id = result.removeprefix("Plan submitted (").removesuffix(")")
        state = self.coordinator.get_request(request_id)
        self.assertIsNotNone(state)
        self.assertEqual(state.type, "plan_approval")
        self.assertEqual(state.sender, "proof_worker")
        self.assertEqual(state.status, "pending")
        self.assertEqual(state.payload, "# Proof Plan: Test")

        msgs = self.coordinator.bus.read_inbox("lead")
        self.assertEqual(msgs[0]["type"], "plan_approval_request")
        self.assertEqual(msgs[0]["content"], "# Proof Plan: Test")
        self.assertEqual(msgs[0]["metadata"]["request_id"], request_id)

    def test_review_plan_updates_state_and_sends_response(self):
        result = self.coordinator.submit_plan("proof_worker", "plan")
        request_id = result.removeprefix("Plan submitted (").removesuffix(")")
        self.coordinator.bus.read_inbox("lead")

        output = self.coordinator.review_plan(
            request_id,
            approve=True,
            feedback="Proceed",
        )
        self.assertEqual(output, "Plan approved")
        self.assertEqual(self.coordinator.get_request(request_id).status, "approved")

        msgs = self.coordinator.bus.read_inbox("proof_worker")
        self.assertEqual(msgs[0]["type"], "plan_approval_response")
        self.assertEqual(msgs[0]["content"], "Proceed")
        self.assertTrue(msgs[0]["metadata"]["approve"])

    def test_request_shutdown_and_response_are_correlated(self):
        output = self.coordinator.request_shutdown("proof_worker")
        self.assertTrue(output.startswith("Shutdown request sent"))
        request = next(iter(self.coordinator.pending_requests.values()))
        self.assertEqual(request.type, "shutdown")
        self.assertEqual(request.status, "pending")

        teammate_inbox = self.coordinator.bus.read_inbox("proof_worker")
        request_id = teammate_inbox[0]["metadata"]["request_id"]
        self.coordinator.bus.send(
            "proof_worker",
            "lead",
            "Shutting down.",
            "shutdown_response",
            {"request_id": request_id, "approve": True},
        )
        self.coordinator.consume_lead_inbox(route_protocol=True)
        self.assertEqual(
            self.coordinator.get_request(request_id).status,
            "approved",
        )

    def test_wrong_response_type_does_not_resolve_request(self):
        result = self.coordinator.submit_plan("proof_worker", "plan")
        request_id = result.removeprefix("Plan submitted (").removesuffix(")")
        self.coordinator.match_response(
            "shutdown_response",
            request_id,
            approve=True,
        )
        self.assertEqual(self.coordinator.get_request(request_id).status, "pending")

    def test_request_plan_is_plain_message(self):
        output = self.coordinator.request_plan("proof_worker", "prove lemma")
        self.assertEqual(output, "Asked proof_worker to submit a plan")
        msgs = self.coordinator.bus.read_inbox("proof_worker")
        self.assertEqual(msgs[0]["type"], "message")
        self.assertEqual(msgs[0]["content"], "Submit plan for: prove lemma")

    def test_format_lead_inbox_routes_and_formats_messages(self):
        self.coordinator.bus.send(
            "proof_worker",
            "lead",
            "progress update",
            "message",
        )
        text = self.coordinator.format_lead_inbox()
        self.assertIn("[proof_worker]", text)
        self.assertIn("[message]", text)
        self.assertIn("progress update", text)
        self.assertEqual(self.coordinator.format_lead_inbox(), "(inbox empty)")

    def test_constructor_migrates_legacy_jsonl_mailbox(self):
        # Use a fresh store path so this test is independent from setUp's store.
        root = self.root / "legacy-case"
        mailbox = root / ".mailboxes"
        mailbox.mkdir(parents=True)
        line = json.dumps(
            {
                "from": "old-worker",
                "to": "lead",
                "content": "legacy hello",
                "type": "message",
                "ts": 7.0,
                "metadata": {},
            }
        )
        (mailbox / "lead.jsonl").write_text(line + "\n", encoding="utf-8")
        store = MessageStore(root / ".state" / "team.db")
        coordinator = TeamCoordinator(
            mailbox_dir=mailbox,
            message_store=store,
            terminal_print=lambda _: None,
        )
        self.assertEqual(
            [m["content"] for m in coordinator.bus.read_inbox("lead")],
            ["legacy hello"],
        )
        self.assertFalse((mailbox / "lead.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
