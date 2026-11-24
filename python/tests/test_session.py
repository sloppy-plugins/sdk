import asyncio
import json
import os
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from sloppy_sdk import SloppyError, SloppyIdentity, SloppySession


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for SDK state")
        time.sleep(0.01)


class Server:
    def __init__(self, **options):
        self.options = options
        self.directory = tempfile.TemporaryDirectory(prefix="sloppy-sdk-")
        self.file = Path(self.directory.name) / "connection.json"
        self.sessions, self.requests, self.hellos = {}, [], []
        self.sockets = set()
        self.lock = threading.RLock()
        self.creates = 0
        self.dropped = False
        self.identity = SloppyIdentity(
            application="test service",
            plugin_id="service",
            directory=Path(self.directory.name) / "server",
        )
        identity = self.identity._load()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        # Each fixture connection performs a fresh handshake. Avoid TLS session
        # tickets racing concurrent SSLSocket reads/writes in the sync server.
        context.num_tickets = 0
        cert_file, key_file = (
            Path(self.directory.name) / "certificate.pem",
            Path(self.directory.name) / "key.pem",
        )
        cert_file.write_text(identity["certificate_pem"])
        key_file.write_text(identity["private_key_pem"])
        key_file.chmod(0o600)
        context.load_cert_chain(cert_file, key_file)
        key_file.unlink()
        cert_file.unlink()
        self.context = context
        self.approval = threading.Event()
        self.server = serve(
            self.handle, "127.0.0.1", 0, ssl=context, ping_interval=None
        )
        self.connection = {
            "protocol_version": 1,
            "service": "SloppyService",
            "instance_id": str(uuid.uuid4()),
            "token": "a" * 64,
            "base_url": f"https://127.0.0.1:{self.server.socket.getsockname()[1]}",
        }
        self.file.write_text(json.dumps(self.connection), encoding="utf-8")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def send(self, socket, body):
        socket.send(json.dumps(body))

    def handle(self, socket):
        with self.lock:
            self.sockets.add(socket)
        session = None
        next_ping = time.monotonic()
        try:
            mode = self.options.get("client_trust", "ready")
            if mode not in {"ready", "pending"}:
                self.send(
                    socket,
                    {"type": "error", "code": mode, "message": "Client was denied"},
                )
                return
            if mode == "pending":
                self.send(socket, {"type": "trust_pending", "timeout_ms": 120000})
                if not self.approval.wait(timeout=3):
                    return
            self.send(socket, {"type": "trust_ready", "protocol_version": 1})
            if (
                socket.request.headers.get(
                    "Authorization", "Bearer " + self.connection["token"]
                )
                != "Bearer " + self.connection["token"]
            ):
                raise AssertionError("Wrong authorization")
            while True:
                try:
                    body = json.loads(socket.recv(timeout=0.03))
                except TimeoutError:
                    if (
                        session
                        and not self.options.get("silent")
                        and time.monotonic() >= next_ping
                    ):
                        self.send(socket, {"type": "ping"})
                        next_ping = time.monotonic() + 0.03
                    continue
                if body["type"] == "hello":
                    with self.lock:
                        self.hellos.append(body)
                        resume = body.get("resume")
                        if resume and (
                            self.options.get("deny_resume")
                            or resume["session_id"] not in self.sessions
                        ):
                            self.send(
                                socket,
                                {
                                    "type": "error",
                                    "code": "session_denied"
                                    if self.options.get("deny_resume")
                                    else "session_unknown",
                                    "message": "Cannot resume",
                                },
                            )
                            continue
                        session = (
                            self.sessions[resume["session_id"]]
                            if resume
                            else {
                                "id": str(uuid.uuid4()),
                                "token": "b" * 64,
                                "items": {},
                                "replies": {},
                            }
                        )
                        self.sessions[session["id"]] = session
                        session["socket"] = socket
                        self.send(
                            socket,
                            {
                                "type": "ready",
                                "protocol_version": 1,
                                "instance_id": self.connection["instance_id"],
                                "session_id": session["id"],
                                "resume_token": session["token"],
                                "heartbeat_interval_ms": 30,
                                "heartbeat_timeout_ms": 150,
                                "pending_reply_ids": list(session["replies"]),
                                "notifications": list(session["items"].values()),
                                **self.options.get("ready", {}),
                            },
                        )
                        if self.options.get("malformed"):
                            socket.send("not json")
                    continue
                if body["type"] != "request":
                    if body["type"] == "received":
                        session["replies"].pop(body["request_id"], None)
                    continue
                with self.lock:
                    self.requests.append(body)
                    reply = session["replies"].get(body["request_id"])
                    if reply is None:
                        if body["op"] == "create":
                            self.creates += 1
                            value = dict(
                                body["notification"],
                                id=body["id"],
                                session_id=session["id"],
                                state="pending",
                                requires_response=body["notification"].get(
                                    "requires_response", False
                                ),
                            )
                            session["items"][body["id"]] = value
                            if self.options.get("after_create"):
                                self.options["after_create"](session, value)
                        elif body["op"] == "list":
                            value = list(session["items"].values())
                        else:
                            value = session["items"][body["id"]]
                            if body["op"] in {"answer", "resolve"}:
                                value = dict(
                                    value,
                                    state="submitted"
                                    if body["op"] == "answer"
                                    else "resolved",
                                    resolution={"answer": body["answer"]},
                                )
                            elif body["op"] == "dismiss":
                                value = dict(
                                    value,
                                    state="cancelled",
                                    cancellation={"reason": body["reason"]},
                                )
                            session["items"][body["id"]] = value
                        reply = {
                            "type": "reply",
                            "request_id": body["request_id"],
                            "ok": True,
                            "value": None if self.options.get("bad_reply") else value,
                        }
                        session["replies"][body["request_id"]] = reply
                    if self.options.get("drop_after_apply") and not self.dropped:
                        self.dropped = True
                        socket.close()
                        return
                    self.send(socket, reply)
                    if body["op"] != "list":
                        self.send(
                            socket,
                            {
                                "type": "notification",
                                "notification": session["items"][body["id"]],
                            },
                        )
        except ConnectionClosed:
            pass
        finally:
            with self.lock:
                self.sockets.discard(socket)

    def drop(self):
        with self.lock:
            sockets = list(self.sockets)
        for socket in sockets:
            socket.close()

    def restart(self):
        with self.lock:
            self.sessions.clear()
            self.connection["instance_id"] = str(uuid.uuid4())
            self.file.write_text(json.dumps(self.connection), encoding="utf-8")
        self.drop()

    def rotate_certificate(self):
        self.identity.regenerate()
        identity = self.identity._load()
        with (
            tempfile.NamedTemporaryFile() as certificate,
            tempfile.NamedTemporaryFile() as key,
        ):
            certificate.write(identity["certificate_pem"].encode())
            key.write(identity["private_key_pem"].encode())
            certificate.flush()
            key.flush()
            self.context.load_cert_chain(certificate.name, key.name)

    def close(self):
        self.server.shutdown()
        self.drop()
        self.thread.join(timeout=3)
        self.directory.cleanup()


class SessionTests(unittest.TestCase):
    def test_version_mismatch_is_meaningful_and_does_not_reconnect(self):
        message = "Plugin version mismatch: testing@2.0.0 is not installed. Installed versions: 1.0.0."
        server, sdk = self.fixture(
            ready={
                "type": "error",
                "code": "plugin_version_mismatch",
                "message": message,
            }
        )
        with self.assertRaises(SloppyError) as error:
            sdk.ready.result(timeout=3)
        self.assertEqual(error.exception.code, "plugin_version_mismatch")
        self.assertIn("testing@2.0.0", str(error.exception))
        self.assertIn("1.0.0", str(error.exception))
        attempts = len(server.hellos)
        time.sleep(0.15)
        self.assertEqual(len(server.hellos), attempts)
        self.assertFalse(sdk.online)

    def fixture(self, **options):
        server = Server(**options)
        self.addCleanup(server.close)
        sdk = SloppySession(
            plugin_id="testing",
            connection_file=server.file,
            identity_directory=Path(server.directory.name) / "client",
            confirm_server=lambda presented: (
                presented.fingerprint == server.identity.fingerprint()
            ),
            retry_delay=0.01,
            max_retry_delay=0.05,
        )
        self.addCleanup(sdk.close)
        return server, sdk

    def assert_error(self, future, code):
        with self.assertRaises(SloppyError) as error:
            future.result(timeout=3)
        self.assertEqual(error.exception.code, code)

    def test_sdk_owns_private_content_free_client_diagnostics(self):
        server, sdk = self.fixture()
        sdk.ready.result(timeout=3)
        secret = "DO_NOT_LOG_BUBBLE_TEXT"
        sdk.send({"title": secret, "message": secret}).created.result(timeout=3)
        session_id = sdk.session_id
        server.drop()
        wait_for(lambda: len(server.hellos) >= 2 and sdk.online)
        self.assertEqual(sdk.session_id, session_id)
        sdk.close()
        contents = sdk.diagnostic_file.read_text(encoding="utf-8")
        records = [json.loads(line) for line in contents.splitlines()]
        for event in ("connect_attempt", "ready", "closed"):
            self.assertTrue(any(record["event"] == event for record in records), event)
        self.assertTrue(
            any(
                record["event"] == "connect_attempt" and record["detail"]["resumed"]
                for record in records
            )
        )
        self.assertTrue(all(record["level"] != "debug" for record in records))
        for value in (secret, server.connection["token"], "b" * 64, "resume_token"):
            self.assertNotIn(value, contents)
        if os.name != "nt":
            self.assertEqual(sdk.diagnostic_file.stat().st_mode & 0o777, 0o600)

    def test_client_log_rotates_with_one_backup(self):
        _, sdk = self.fixture()
        sdk.ready.result(timeout=3)
        path = sdk.diagnostic_file
        path.write_bytes(b"x" * (1024 * 1024))
        sdk.close()
        self.assertLess(path.stat().st_size, 1024 * 1024)
        self.assertEqual(Path(str(path) + ".1").stat().st_size, 1024 * 1024)
        self.assertIn('"event":"closed"', path.read_text(encoding="utf-8"))

    def test_shared_verbosity_changes_recording_without_reconnecting(self):
        server, sdk = self.fixture()
        sdk.ready.result(timeout=3)
        bubble = sdk.send({"title": "Quiet default"}).created.result(timeout=3)
        config = Path(server.directory.name) / "logs/verbosity.json"
        records = [
            json.loads(line) for line in sdk.diagnostic_file.read_text().splitlines()
        ]
        self.assertTrue(all(record["level"] == "info" for record in records))
        config.write_text(json.dumps({"level": "debug"}))
        sdk.get(bubble["id"]).result(timeout=3)
        records = [
            json.loads(line) for line in sdk.diagnostic_file.read_text().splitlines()
        ]
        self.assertTrue(
            any(
                record["event"] == "sending" and record["level"] == "debug"
                for record in records
            )
        )
        self.assertTrue(
            any(
                record["event"] == "received" and record["level"] == "debug"
                for record in records
            )
        )
        config.write_text(json.dumps({"level": "error"}))
        size = sdk.diagnostic_file.stat().st_size
        sdk.get(bubble["id"]).result(timeout=3)
        self.assertEqual(sdk.diagnostic_file.stat().st_size, size)
        self.assertEqual(len(server.hellos), 1)
        config.write_text(json.dumps({"level": ["debug"]}))
        sdk.get(bubble["id"]).result(timeout=3)
        self.assertEqual(sdk.diagnostic_error.code, "diagnostic_error")
        self.assertEqual(sdk.diagnostic_file.stat().st_size, size)
        config.write_text(json.dumps({"level": "info"}))

    def test_client_log_write_failure_does_not_stop_reconnection(self):
        server, sdk = self.fixture()
        sdk.ready.result(timeout=3)
        path = sdk.diagnostic_file
        path.unlink()
        path.mkdir()
        failures = []
        sdk.on("diagnostic_error", failures.append)
        server.drop()
        wait_for(lambda: len(server.hellos) >= 2 and sdk.online and failures)
        self.assertEqual(sdk.diagnostic_error.code, "diagnostic_error")
        self.assertTrue(
            all(
                str(error) == "Cannot write private Sloppy session log"
                for error in failures
            )
        )

    def test_proposals_do_not_claim_completion(self):
        _, sdk = self.fixture()
        bubble = sdk.send({"title": "Approval", "requires_response": True})
        bubble.created.result(timeout=3)
        answer = {"value": True}
        self.assertEqual(
            sdk.answer(bubble.id, answer).result(timeout=3)["state"], "submitted"
        )
        self.assertEqual(bubble.result(timeout=3), dict(answer, source="testing"))
        self.assertEqual(
            sdk.acknowledge(bubble.id, "accepted").result(timeout=3)["state"],
            "submitted",
        )
        self.assertEqual(
            sdk.resolve(bubble.id, answer).result(timeout=3)["state"], "resolved"
        )

    def test_information_and_dismissal(self):
        _, sdk = self.fixture()
        self.assertIsNone(sdk.send({"title": "Info"}).result(timeout=3))
        bubble = sdk.send({"title": "Question", "requires_response": True})
        bubble.created.result(timeout=3)
        sdk.dismiss(bubble.id, "Native close").result(timeout=3)
        self.assert_error(bubble, "dismissed")

    def test_dropped_reply_is_retried_with_the_same_id(self):
        server, sdk = self.fixture(drop_after_apply=True)
        sdk.send({"title": "Once"}).created.result(timeout=3)
        self.assertEqual(server.creates, 1)
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(
            server.requests[0]["request_id"], server.requests[1]["request_id"]
        )
        self.assertEqual(server.hellos[1]["resume"]["session_id"], sdk.session_id)

    def test_resumed_snapshot_delivers_missed_answers(self):
        server, sdk = self.fixture()
        bubble = sdk.send({"title": "Question", "requires_response": True})
        bubble.created.result(timeout=3)
        answer = {"value": "while disconnected"}
        with server.lock:
            owner = server.sessions[sdk.session_id]
            owner["items"][bubble.id] = dict(
                owner["items"][bubble.id],
                state="submitted",
                resolution={"answer": answer},
            )
        server.drop()
        self.assertEqual(bubble.result(timeout=3), answer)
        self.assertEqual(sdk.session_id, owner["id"])

    def test_restart_does_not_replay_old_bubbles(self):
        server, sdk = self.fixture()
        bubble = sdk.send({"title": "Old", "requires_response": True})
        bubble.created.result(timeout=3)
        old_id = sdk.session_id
        server.restart()
        self.assert_error(bubble, "session_lost")
        wait_for(lambda: sdk.online and sdk.session_id != old_id)
        sdk.send({"title": "New"}).created.result(timeout=3)
        self.assertEqual(server.creates, 2)

    def test_cached_creation_reply_cannot_overwrite_newer_notification(self):
        answer = {"value": "Already submitted"}

        def after_create(owner, item):
            owner["items"][item["id"]] = dict(
                item, state="submitted", resolution={"answer": answer}
            )

        _, sdk = self.fixture(drop_after_apply=True, after_create=after_create)
        states = []
        sdk.on("notification", lambda item: states.append(item["state"]))
        bubble = sdk.send({"title": "Question", "requires_response": True})
        self.assertEqual(bubble.result(timeout=3), answer)
        bubble.created.result(timeout=3)
        wait_for(lambda: "submitted" in states)
        self.assertNotIn("pending", states[states.index("submitted") :])

    def test_forgotten_session_reestablishes_ownership_without_replay(self):
        server, sdk = self.fixture()
        bubble = sdk.send({"title": "Old", "requires_response": True})
        bubble.created.result(timeout=3)
        old_id = sdk.session_id
        server.sessions.clear()
        server.drop()
        self.assert_error(bubble, "session_lost")
        wait_for(lambda: sdk.online and sdk.session_id != old_id)
        self.assertEqual(server.creates, 1)

    def test_invalid_credentials_fail_instead_of_retrying_forever(self):
        server, sdk = self.fixture(deny_resume=True)
        bubble = sdk.send({"title": "Old", "requires_response": True})
        bubble.created.result(timeout=3)
        server.drop()
        self.assert_error(bubble, "session_denied")
        self.assertFalse(sdk.online)
        self.assertIn("could not resume", sdk.last_error)
        self.assertIn("Restart this plugin", sdk.last_error)
        self.assertEqual(len(server.hellos), 2)

    def test_heartbeat_failure_is_reported_and_retried(self):
        server, sdk = self.fixture(silent=True)
        errors = []
        sdk.on(
            "status",
            lambda status: errors.append(status["error"]) if status["error"] else None,
        )
        sdk.ready.result(timeout=3)
        wait_for(lambda: any("heartbeat" in error for error in errors))
        wait_for(lambda: len(server.hellos) > 1)

    def test_malformed_reply_does_not_leave_a_future_hanging(self):
        _, sdk = self.fixture(bad_reply=True)
        bubble = sdk.send({"title": "Bad reply"})
        self.assert_error(bubble.created, "protocol_error")
        self.assert_error(bubble, "protocol_error")

    def test_malformed_cancellation_does_not_strand_a_future(self):
        def after_create(_owner, item):
            item["state"], item["cancellation"] = "cancelled", 17

        _, sdk = self.fixture(after_create=after_create)
        bubble = sdk.send({"title": "Bad cancellation"})
        self.assert_error(bubble.created, "protocol_error")
        self.assert_error(bubble, "protocol_error")

    def test_malformed_handshake_fails_explicitly(self):
        _, sdk = self.fixture(ready={"heartbeat_timeout_ms": -1})
        self.assert_error(sdk.ready, "protocol_error")

    def test_invalid_json_frame_reports_a_protocol_error(self):
        _, sdk = self.fixture(malformed=True)
        bubble = sdk.send({"title": "Malformed frame"})
        self.assert_error(bubble.created, "protocol_error")
        self.assertTrue(sdk.last_error)

    def test_lost_acknowledgements_are_cleaned_up_after_resume(self):
        server, sdk = self.fixture()
        sdk.send({"title": "Old"}).result(timeout=3)
        owner = server.sessions[sdk.session_id]
        orphan = str(uuid.uuid4())
        with server.lock:
            owner["replies"][orphan] = {
                "type": "reply",
                "request_id": orphan,
                "ok": True,
                "value": None,
            }
        server.drop()
        wait_for(lambda: len(server.hellos) > 1 and orphan not in owner["replies"])

    def test_callbacks_can_wait_for_other_operations_without_blocking_transport(self):
        _, sdk = self.fixture()
        sdk.ready.result(timeout=3)
        result, finished = [], threading.Event()

        def callback(_future):
            result.append(
                sdk.send({"title": "From callback"}).created.result(timeout=3)["title"]
            )
            finished.set()

        sdk.list().add_done_callback(callback)
        self.assertTrue(finished.wait(3))
        self.assertEqual(result, ["From callback"])

    def test_all_public_futures_are_awaitable(self):
        _, sdk = self.fixture()

        async def run():
            await sdk.ready
            bubble = sdk.send({"title": "Async"})
            await bubble.created
            self.assertIsNone(await bubble)
            self.assertEqual(len(await sdk.list()), 1)

        asyncio.run(run())

    def test_queued_inputs_are_snapshots_and_invalid_json_is_rejected(self):
        server, sdk = self.fixture()
        payload = {"title": "Original", "details": {"nested": "original"}}
        bubble = sdk.send(payload)
        payload["details"]["nested"] = "mutated"
        bubble.created.result(timeout=3)
        self.assertEqual(
            server.requests[0]["notification"]["details"]["nested"], "original"
        )
        invalid = sdk.send({"value": float("nan")})
        self.assert_error(invalid.created, "invalid_input")
        self.assert_error(invalid, "invalid_input")
        cycle = {}
        cycle["self"] = cycle
        circular = sdk.send({"details": cycle})
        self.assert_error(circular.created, "invalid_input")
        self.assert_error(circular, "invalid_input")

    def test_close_rejects_all_queued_work(self):
        _, sdk = self.fixture()
        bubble = sdk.send({"title": "Queued"})
        sdk.close()
        self.assert_error(bubble, "closed")
        self.assert_error(bubble.created, "closed")
        self.assert_error(sdk.list(), "closed")


@unittest.skipUnless(
    os.environ.get("SLOPPY_SDK_SERVICE_BINARY"),
    "Set SLOPPY_SDK_SERVICE_BINARY for real-service integration",
)
class ServiceTests(unittest.TestCase):
    def test_real_service_ownership_and_explicit_completion(self):
        with tempfile.TemporaryDirectory(prefix="sloppy-sdk-integration-") as directory:
            file = Path(directory) / "connection.json"
            owner_identity = SloppyIdentity(
                application="sdk.integration",
                plugin_id="copilot",
                directory=Path(directory) / "owner",
            )
            other_identity = SloppyIdentity(
                application="copilot",
                plugin_id="copilot",
                directory=Path(directory) / "other",
            )
            trusted = [
                {
                    "fingerprint": identity.fingerprint(),
                    "name": "integration client",
                    "first_seen_at_ms": int(time.time() * 1000),
                    "last_connected_at_ms": int(time.time() * 1000),
                    "address": "127.0.0.1",
                }
                for identity in (owner_identity, other_identity)
            ]
            (Path(directory) / "client-trust.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "allow_remote_clients": False,
                        "ignore_unknown_clients": False,
                        "trusted_clients": {
                            entry["fingerprint"]: entry for entry in trusted
                        },
                        "rejected_clients": {},
                    }
                )
            )
            process = subprocess.Popen(
                [
                    os.environ["SLOPPY_SDK_SERVICE_BINARY"],
                    "--port",
                    "0",
                    "--connection-file",
                    str(file),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            owner = other = None
            try:
                wait_for(lambda: file.is_file() or process.poll() is not None)
                self.assertIsNone(process.poll())
                owner = SloppySession(
                    plugin_id="copilot",
                    application="sdk.integration",
                    connection_file=file,
                    identity_directory=owner_identity.directory,
                    confirm_server=lambda _presented: True,
                )
                other = SloppySession(
                    plugin_id="copilot",
                    connection_file=file,
                    identity_directory=other_identity.directory,
                    confirm_server=lambda _presented: True,
                )
                owner.ready.result(timeout=3)
                other.ready.result(timeout=3)
                bubble = owner.send(
                    {
                        "kind": "question",
                        "title": "Owned",
                        "message": "Only this session can answer",
                        "requires_response": True,
                        "response": {
                            "value_type": "text",
                            "allow_custom": True,
                            "choices": [],
                        },
                    }
                )
                self.assertEqual(
                    bubble.created.result(timeout=3)["session_id"], owner.session_id
                )
                with self.assertRaises(SloppyError) as denied:
                    other.get(bubble.id).result(timeout=3)
                self.assertEqual(denied.exception.status, 404)
                self.assertEqual(other.list().result(timeout=3), [])
                self.assertEqual(
                    owner.answer(bubble.id, {"value": "Proposal"}).result(timeout=3)[
                        "state"
                    ],
                    "submitted",
                )
                self.assertEqual(bubble.result(timeout=3)["value"], "Proposal")
                self.assertEqual(
                    owner.acknowledge(bubble.id, "accepted").result(timeout=3)["state"],
                    "submitted",
                )
                self.assertEqual(
                    owner.resolve(bubble.id, {"value": "Completed"}).result(timeout=3)[
                        "state"
                    ],
                    "resolved",
                )
                self.assertEqual(
                    owner.get(bubble.id).result(timeout=3)["resolution"]["answer"][
                        "source"
                    ],
                    "sdk.integration",
                )
            finally:
                if owner:
                    owner.close()
                if other:
                    other.close()
                process.terminate()
                process.wait(timeout=5)
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
