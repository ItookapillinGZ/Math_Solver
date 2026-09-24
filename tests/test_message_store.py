from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_runtime.team import MessageStore


class MessageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "team.db"
        self.store = MessageStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_send_and_consume_preserve_legacy_message_shape(self):
        self.store.send(
            "worker",
            "lead",
            "hello",
            "message",
            {"request_id": "req_1", "unicode": "证明"},
            ts=123.5,
        )
        records = self.store.consume_inbox("lead")
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0].to_message(),
            {
                "from": "worker",
                "to": "lead",
                "content": "hello",
                "type": "message",
                "ts": 123.5,
                "metadata": {"request_id": "req_1", "unicode": "证明"},
            },
        )

    def test_consume_is_once_only(self):
        self.store.send("worker", "lead", "one")
        self.assertEqual(len(self.store.consume_inbox("lead")), 1)
        self.assertEqual(self.store.consume_inbox("lead"), [])
        self.assertEqual(self.store.unread_count("lead"), 0)
        self.assertEqual(self.store.total_count(), 1)

    def test_unread_message_survives_store_restart(self):
        self.store.send("worker", "lead", "survive")
        reopened = MessageStore(self.db_path)
        records = reopened.consume_inbox("lead")
        self.assertEqual([record.content for record in records], ["survive"])

    def test_fifo_order_is_stable(self):
        for content in ("first", "second", "third"):
            self.store.send("worker", "lead", content)
        records = self.store.consume_inbox("lead")
        self.assertEqual(
            [record.content for record in records],
            ["first", "second", "third"],
        )

    def test_concurrent_consumers_do_not_duplicate_messages(self):
        for index in range(40):
            self.store.send("worker", "lead", f"m{index}")

        stores = [MessageStore(self.db_path) for _ in range(4)]
        barrier = threading.Barrier(len(stores))
        results: list[str] = []
        lock = threading.Lock()

        def consume(store: MessageStore) -> None:
            barrier.wait()
            batch = store.consume_inbox("lead")
            with lock:
                results.extend(record.content for record in batch)

        threads = [threading.Thread(target=consume, args=(store,)) for store in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(results), 40)
        self.assertEqual(len(set(results)), 40)
        self.assertEqual(self.store.unread_count("lead"), 0)

    def test_concurrent_senders_keep_every_message(self):
        stores = [MessageStore(self.db_path) for _ in range(4)]
        barrier = threading.Barrier(len(stores))

        def produce(worker_index: int, store: MessageStore) -> None:
            barrier.wait()
            for item in range(25):
                store.send(f"worker{worker_index}", "lead", f"{worker_index}:{item}")

        threads = [
            threading.Thread(target=produce, args=(index, store))
            for index, store in enumerate(stores)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        records = self.store.consume_inbox("lead")
        self.assertEqual(len(records), 100)
        self.assertEqual(len({record.content for record in records}), 100)

    def test_legacy_jsonl_is_migrated_and_removed(self):
        legacy = self.root / ".mailboxes"
        legacy.mkdir()
        inbox = legacy / "lead.jsonl"
        messages = [
            {
                "from": "a",
                "to": "lead",
                "content": "first",
                "type": "message",
                "ts": 1.0,
                "metadata": {},
            },
            {
                "from": "b",
                "to": "lead",
                "content": "second",
                "type": "message",
                "ts": 2.0,
                "metadata": {"x": 1},
            },
        ]
        inbox.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in messages) + "\n",
            encoding="utf-8",
        )

        result = self.store.migrate_legacy_mailboxes(legacy)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["errors"], 0)
        self.assertFalse(inbox.exists())
        self.assertEqual(
            [r.content for r in self.store.consume_inbox("lead")],
            ["first", "second"],
        )

    def test_legacy_migration_is_idempotent_after_recreated_file(self):
        legacy = self.root / ".mailboxes"
        legacy.mkdir()
        inbox = legacy / "lead.jsonl"
        line = json.dumps(
            {
                "from": "a",
                "to": "lead",
                "content": "same",
                "type": "message",
                "ts": 1.0,
                "metadata": {},
            }
        )
        inbox.write_text(line + "\n", encoding="utf-8")
        first = self.store.migrate_legacy_mailboxes(legacy)
        inbox.write_text(line + "\n", encoding="utf-8")
        second = self.store.migrate_legacy_mailboxes(legacy)

        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(self.store.total_count(), 1)

    def test_malformed_legacy_file_is_preserved_for_inspection(self):
        legacy = self.root / ".mailboxes"
        legacy.mkdir()
        inbox = legacy / "lead.jsonl"
        valid = json.dumps(
            {
                "from": "a",
                "to": "lead",
                "content": "valid",
                "type": "message",
                "ts": 1.0,
                "metadata": {},
            }
        )
        inbox.write_text(valid + "\n{not-json}\n", encoding="utf-8")

        first = self.store.migrate_legacy_mailboxes(legacy)
        second = self.store.migrate_legacy_mailboxes(legacy)
        self.assertEqual(first["imported"], 1)
        self.assertGreaterEqual(first["errors"], 1)
        self.assertTrue(inbox.exists())
        self.assertEqual(second["imported"], 0)
        self.assertGreaterEqual(second["duplicates"], 1)
        self.assertEqual(self.store.total_count(), 1)


if __name__ == "__main__":
    unittest.main()
