import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_session import Server, wait_for

from sloppy_sdk import SloppyError, SloppySession, discover


class ErrorTests(unittest.TestCase):
    def test_missing_discovery_explains_service_setup_and_custom_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SloppyError) as caught:
                discover(Path(directory) / "connection.json")
            self.assertEqual(caught.exception.code, "service_unavailable")
            self.assertIn("Enable Sloppy service server", str(caught.exception))
            self.assertIn("custom connection file", str(caught.exception))
            self.assertNotIn("Sloppy is not running", str(caught.exception))
            self.assertNotIn("No such file", str(caught.exception))

    def test_bad_json_gives_recovery_without_echoing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "connection.json"
            file.write_text('{"token":"DO_NOT_ECHO_PRIVATE_TOKEN", broken}')
            with self.assertRaises(SloppyError) as caught:
                discover(file)
            self.assertEqual(caught.exception.code, "discovery_error")
            self.assertIn("Restart Sloppy", str(caught.exception))
            self.assertNotIn("DO_NOT_ECHO_PRIVATE_TOKEN", str(caught.exception))

    def test_permission_failure_does_not_claim_service_is_disabled(self):
        with patch.object(Path, "read_text", side_effect=PermissionError()):
            with self.assertRaises(SloppyError) as caught:
                discover("/configured/connection.json")
        self.assertEqual(caught.exception.code, "discovery_error")
        self.assertIn("permission denied", str(caught.exception))
        self.assertIn("permissions", str(caught.exception))
        self.assertNotIn("service server", str(caught.exception))

    def test_stale_service_address_reports_reachability_and_retries(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            file = Path(directory) / "connection.json"
            file.write_text(
                json.dumps(
                    {
                        "protocol_version": 1,
                        "service": "SloppyService",
                        "token": "a" * 64,
                        "instance_id": "12345678-1234-1234-1234-123456789abc",
                        "base_url": f"https://127.0.0.1:{unavailable.getsockname()[1]}",
                    }
                )
            )
            unavailable.listen()
            unavailable.close()
            session = SloppySession(
                plugin_id="testing",
                connection_file=file,
                identity_directory=Path(directory) / "identity",
                retry_delay=0.01,
            )
            try:
                wait_for(lambda: session.last_error is not None)
                self.assertIn("Cannot reach the Sloppy service", session.last_error)
                self.assertIn("Reconnecting automatically", session.last_error)
                self.assertFalse(session.ready.done())
            finally:
                session.close()

    def test_service_rejections_and_disabled_plugin_have_distinct_guidance(self):
        for code, guidance in [
            ("client_rejected", "Rejected clients"),
            ("unknown_client_ignored", "Ignore unknown clients"),
            ("client_trust_busy", "pending approvals"),
            ("plugin_disabled", "sloppy plugins enable testing"),
        ]:
            with self.subTest(code=code):
                server = Server(client_trust=code)
                session = SloppySession(
                    plugin_id="testing",
                    connection_file=server.file,
                    identity_directory=Path(server.directory.name) / "client",
                    retry_delay=0.01,
                    confirm_server=lambda _service: True,
                )
                try:
                    wait_for(lambda: session.last_error is not None)
                    self.assertIn(guidance, session.last_error)
                    self.assertNotIn("Client was denied", session.last_error)
                    self.assertFalse(session.online)
                finally:
                    session.close()
                    server.close()
