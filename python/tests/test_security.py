import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_session import Server, wait_for

from sloppy_sdk import (
    SloppyError,
    SloppyIdentity,
    SloppySession,
    SloppyTrustRequiredError,
    certificate_fingerprint,
    inspect_service,
)
from sloppy_sdk.security import canonical_endpoint


class SecurityTests(unittest.TestCase):
    def test_managed_identity_directory_is_shared_and_explicit_directory_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            managed = str(Path(temporary) / "managed")
            explicit = Path(temporary) / "explicit"
            with patch.dict(os.environ, {"SLOPPY_IDENTITY_DIRECTORY": managed}):
                self.assertEqual(
                    SloppyIdentity(
                        application="Example", plugin_id="example"
                    ).directory,
                    Path(managed),
                )
                self.assertEqual(
                    SloppyIdentity(
                        application="Example", plugin_id="example", directory=explicit
                    ).directory,
                    explicit,
                )

    def fixture(self, **options):
        server = Server(**options)
        self.addCleanup(server.close)
        return server

    def session(self, server, **options):
        session = SloppySession(
            plugin_id="testing",
            endpoint=server.connection["base_url"],
            connection_file=server.file,
            identity_directory=Path(server.directory.name) / "client",
            retry_delay=0.01,
            **options,
        )
        self.addCleanup(session.close)
        return session

    def test_unknown_server_requires_explicit_confirmation_before_http_or_client_approval(
        self,
    ):
        server = self.fixture()
        session = self.session(server)
        with self.assertRaises(SloppyTrustRequiredError) as error:
            session.ready.result(timeout=3)
        self.assertEqual(error.exception.code, "server_trust_required")
        self.assertIsNone(error.exception.known_fingerprint)
        self.assertEqual(
            error.exception.presented.fingerprint, server.identity.fingerprint()
        )
        self.assertEqual(server.hellos, [])
        self.assertEqual(len(server.sockets), 0)
        self.assertEqual(session.identity.list_known_services(), [])

    def test_client_approval_gates_hello(self):
        server = self.fixture(client_trust="pending")
        confirmed = []

        def confirm(service):
            confirmed.append(service.fingerprint)
            return service.fingerprint == server.identity.fingerprint()

        session = self.session(server, confirm_server=confirm)
        wait_for(lambda: len(server.sockets) == 1)
        time.sleep(0.1)
        self.assertEqual(confirmed, [server.identity.fingerprint()])
        self.assertFalse(session.online)
        self.assertEqual(server.hellos, [])
        server.approval.set()
        session.ready.result(timeout=3)
        self.assertEqual(server.hellos[0]["type"], "hello")

    def test_remembered_server_has_no_prompt_and_changed_server_requires_explicit_override(
        self,
    ):
        server = self.fixture()
        first = self.session(server, confirm_server=lambda _service: True)
        first.ready.result(timeout=3)
        known = first.identity.list_known_services()[0]
        first.close()

        def no_prompt(_service):
            self.fail("Remembered or changed server must not prompt")

        remembered = self.session(server, confirm_server=no_prompt)
        remembered.ready.result(timeout=3)
        remembered.close()
        server.rotate_certificate()
        changed = self.session(server, confirm_server=no_prompt)
        with self.assertRaises(SloppyTrustRequiredError) as error:
            changed.ready.result(timeout=3)
        self.assertEqual(error.exception.code, "server_identity_changed")
        self.assertEqual(error.exception.known_fingerprint, known["fingerprint"])
        with self.assertRaises(SloppyError):
            changed.identity.remember_service(error.exception.presented)
        changed.identity.remember_service(
            error.exception.presented, known["fingerprint"]
        )
        replaced = self.session(server, confirm_server=no_prompt)
        replaced.ready.result(timeout=3)
        self.assertEqual(
            replaced.identity.list_known_services()[0]["fingerprint"],
            server.identity.fingerprint(),
        )

    def test_server_rotation_between_probe_and_upgrade_cannot_send_application_headers(
        self,
    ):
        server = self.fixture()

        def confirm(_service):
            server.rotate_certificate()
            return True

        session = self.session(server, confirm_server=confirm)
        with self.assertRaises(SloppyError) as error:
            session.ready.result(timeout=3)
        self.assertEqual(error.exception.code, "server_identity_changed")
        self.assertEqual(server.hellos, [])
        self.assertEqual(len(server.sockets), 0)

    def test_client_denials_are_fatal_and_do_not_repeat_approval_requests(self):
        for code in ("client_rejected", "unknown_client_ignored", "client_trust_busy"):
            with self.subTest(code=code):
                server = self.fixture(client_trust=code)
                session = self.session(server, confirm_server=lambda _service: True)
                with self.assertRaises(SloppyError) as error:
                    session.ready.result(timeout=3)
                self.assertEqual(error.exception.code, code)
                attempts = session._attempts
                time.sleep(0.1)
                self.assertEqual(session._attempts, attempts)
                self.assertEqual(server.hellos, [])

    def test_rotation_closes_own_session_and_keeps_service_pins(self):
        server = self.fixture()
        first = self.session(server, confirm_server=lambda _service: True)
        first.ready.result(timeout=3)
        old = first.identity.fingerprint()
        new = first.regenerate_certificate()
        self.assertNotEqual(old, new)
        self.assertFalse(first.online)
        self.assertEqual(len(first.identity.list_known_services()), 1)
        replaced = self.session(server)
        replaced.ready.result(timeout=3)
        self.assertEqual(replaced.identity.fingerprint(), new)

    def test_private_persistence_corruption_and_compare_and_swap_removal(self):
        server = self.fixture()
        session = self.session(server, confirm_server=lambda _service: True)
        session.ready.result(timeout=3)
        known = session.identity.list_known_services()[0]
        with self.assertRaises(SloppyError):
            session.identity.remove_known_service(
                server.connection["base_url"], "wrong"
            )
        session.identity.remove_known_service(
            server.connection["base_url"], known["fingerprint"]
        )
        self.assertEqual(session.identity.list_known_services(), [])
        file = session.identity.directory / "identity.json"
        if os.name != "nt":
            for name in ("identity.json", "known_sloppy"):
                self.assertEqual(
                    (session.identity.directory / name).stat().st_mode & 0o777, 0o600
                )
        original = file.read_text()
        file.write_text("null")
        with self.assertRaises(SloppyError) as error:
            session.identity.fingerprint()
        self.assertEqual(error.exception.code, "identity_error")
        file.write_text(original)
        value = json.loads(original)
        value["private_key_pem"] = server.identity._load()["private_key_pem"]
        file.write_text(json.dumps(value))
        with self.assertRaises(SloppyError):
            session.identity.fingerprint()

    def test_concurrent_generation_reuses_one_identity(self):
        with tempfile.TemporaryDirectory() as directory:

            def fingerprint(_index):
                return SloppyIdentity(
                    application="test", plugin_id="test", directory=directory
                ).fingerprint()

            with ThreadPoolExecutor(max_workers=8) as executor:
                fingerprints = list(executor.map(fingerprint, range(8)))
            self.assertEqual(len(set(fingerprints)), 1)

    def test_certificate_names_enforce_the_common_name_byte_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = SloppyIdentity(
                application="x" * 64, plugin_id="test", directory=directory
            )
            self.assertTrue(identity.fingerprint().startswith("SHA256:"))
            for name in ("x" * 65, "\u00e9" * 33, "bad\nname"):
                identity.name = name
                with self.assertRaises(SloppyError) as error:
                    identity.regenerate()
                self.assertEqual(error.exception.code, "invalid_input")

    def test_plaintext_paths_credentials_and_invalid_ports_are_not_service_endpoints(
        self,
    ):
        for endpoint in (
            "http://127.0.0.1:8787",
            "https://user@localhost",
            "https://localhost/path",
            "https://localhost/?secret=x",
            "https://localhost/#x",
            "https://localhost:0",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(SloppyError):
                    canonical_endpoint(endpoint)
        self.assertEqual(
            canonical_endpoint("https://LOCALHOST:443/"), "https://localhost"
        )
        self.assertEqual(canonical_endpoint("https://[::1]:8787"), "https://[::1]:8787")
        self.assertEqual(
            certificate_fingerprint(b"abc"),
            "SHA256:ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0",
        )

    def test_inspection_reports_certificate_without_opening_an_application_session(
        self,
    ):
        server = self.fixture()
        presented = inspect_service(server.connection["base_url"])
        self.assertEqual(presented.fingerprint, server.identity.fingerprint())
        self.assertEqual(server.hellos, [])
        self.assertEqual(len(server.sockets), 0)
