import json
import tempfile
import threading
import time
import unittest
import uuid
from copy import deepcopy

from sloppy_sdk import SloppyActionsProvider, SloppyError


class Socket:
    def __init__(self):
        self.sent = []

    def send(self, value):
        self.sent.append(json.loads(value))

    def close(self):
        pass


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Action did not complete")
        time.sleep(0.01)


class ActionsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.provider = SloppyActionsProvider(
            plugin_id="test",
            name="Test",
            descriptor={"id": "test", "name": "Test", "version": "1"},
            identity_directory=self.directory.name,
        )
        self.addCleanup(self.provider.close)
        self.provider.provider_id = str(uuid.uuid4())
        self.provider._socket = Socket()
        self.provider.online = True

    def frame(self, sequence=1):
        return dict(
            type="invoke",
            invocation_id=str(uuid.uuid4()),
            invoke_sequence=sequence,
            execution_epoch=self.provider.execution_epoch,
            provider_id=self.provider.provider_id,
            catalog_revision=self.provider.catalog_revision,
            action_key="count",
            input={},
            timeout_ms=1000,
        )

    def test_duplicate_delivery_never_reexecutes_even_after_result_eviction(self):
        calls = []
        self.provider.register_action(
            {"key": "count", "title": "Count"},
            lambda _input, context: (
                calls.append(context.invocation_id) or {"status": "succeeded"}
            ),
        )
        frame = self.frame()
        self.provider._invoke(frame)
        wait_for(lambda: frame["invocation_id"] in self.provider._results)
        self.provider._invoke(frame)
        self.assertEqual(len(calls), 1)
        changed = deepcopy(frame)
        changed["input"] = {"different": True}
        with self.assertRaisesRegex(SloppyError, "immutable"):
            self.provider._invoke(changed)
        with self.provider._lock:
            self.provider._results.clear()
            self.provider._fingerprints.clear()
        self.provider._invoke(frame)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            self.provider._results[frame["invocation_id"]]["status"], "outcome_unknown"
        )

    def test_active_duplicates_and_cancellation_are_cooperative(self):
        started = threading.Event()
        calls = []

        def run(_input, context):
            calls.append(1)
            started.set()
            context.cancelled.wait(2)
            context.check_cancelled()
            return {"status": "succeeded"}

        self.provider.register_action({"key": "count", "title": "Count"}, run)
        frame = self.frame()
        self.provider._invoke(frame)
        self.assertTrue(started.wait(1))
        self.provider._invoke(frame)
        self.assertEqual(calls, [1])
        self.provider._active[frame["invocation_id"]].cancelled.set()
        wait_for(lambda: frame["invocation_id"] in self.provider._results)
        self.assertEqual(
            self.provider._results[frame["invocation_id"]]["status"], "cancelled"
        )

    def test_invalid_input_and_wrong_generation_do_not_reach_handlers(self):
        calls = []
        self.provider.register_action(
            {"key": "count", "title": "Count"}, lambda *_: calls.append(1)
        )
        frame = self.frame()
        frame["execution_epoch"] = str(uuid.uuid4())
        with self.assertRaises(SloppyError):
            self.provider._invoke(frame)
        frame = self.frame()
        frame["input"] = {"unexpected": "value"}
        self.provider._invoke(frame)
        self.assertEqual(calls, [])
        self.assertEqual(
            self.provider._results[frame["invocation_id"]]["status"], "failed"
        )

    def test_catalog_exposes_input_bubbles_and_rejects_hidden_or_executable_schemas(
        self,
    ):
        for definition in [
            {
                "key": "count",
                "title": "Count",
                "input_schema": {"$ref": "https://example.test/schema"},
            },
            {"key": "count", "title": "Count", "interaction": "input_bubble"},
            {"key": "count", "title": "Count", "title_script": "run me"},
        ]:
            with self.assertRaises(SloppyError):
                self.provider.register_action(
                    definition, lambda *_: {"status": "succeeded"}
                )
        self.provider.register_action(
            {
                "key": "count",
                "title": "Count",
                "interaction": "input_bubble",
                "prompt": {"type": "questionnaire", "parameters": {"questions": []}},
            },
            lambda *_: {"status": "succeeded"},
        )
        self.assertEqual(
            self.provider._definitions["count"]["interaction"], "input_bubble"
        )
