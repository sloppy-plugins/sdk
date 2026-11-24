"""Optional public-API integration against an externally supplied Sloppy service."""

import http.client
import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from sloppy_sdk import (
    SloppyActionsProvider,
    SloppyError,
    SloppyIdentity,
    inspect_service,
)

BINARY = os.environ.get("SLOPPY_SDK_SERVICE_BINARY")


def wait_for(check):
    deadline = time.monotonic() + 6
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("Timed out waiting for the test service")
        time.sleep(0.02)


@unittest.skipUnless(
    BINARY, "Set SLOPPY_SDK_SERVICE_BINARY for public service integration"
)
class RealActionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sloppy-provider-service-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.file = self.root / "connection.json"
        self.identity = SloppyIdentity(
            application="Actions test",
            plugin_id="testing",
            directory=self.root / "client",
        )
        fingerprint = self.identity.fingerprint()
        (self.root / "client-trust.json").write_text(
            json.dumps(
                dict(
                    version=1,
                    allow_remote_clients=False,
                    ignore_unknown_clients=False,
                    trusted_clients={
                        fingerprint: dict(
                            fingerprint=fingerprint,
                            name="Actions test",
                            first_seen_at_ms=1,
                            last_connected_at_ms=1,
                            address="127.0.0.1",
                        )
                    },
                    rejected_clients={},
                )
            )
        )
        self.server = subprocess.Popen(
            [BINARY, "--port", "0", "--connection-file", str(self.file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_service)
        wait_for(self.file.exists)
        self.connection = json.loads(self.file.read_text())
        self.service = inspect_service(self.connection["base_url"])
        self.identity.remember_service(self.service)
        self.providers = []
        self.addCleanup(self.close_providers)

    def stop_service(self):
        if self.server.poll() is None:
            self.server.terminate()
            self.server.wait(timeout=5)
        self.server.stderr.close()

    def close_providers(self):
        for provider in reversed(self.providers):
            provider.close()

    def api(self, method, path, body=None, ok=True):
        from urllib.parse import urlsplit

        endpoint = urlsplit(self.connection["base_url"])
        connection = http.client.HTTPSConnection(
            endpoint.hostname,
            endpoint.port,
            context=self.identity._client_context(self.service.fingerprint),
            timeout=5,
        )
        try:
            connection.request(
                method,
                path,
                body=json.dumps(body) if body is not None else None,
                headers={
                    "Authorization": "Bearer " + self.connection["token"],
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            value = json.loads(response.read())
            self.assertEqual(response.status < 300, ok, value)
            return value
        finally:
            connection.close()

    def manifest(self, version):
        return dict(
            manifest_version=1,
            protocol_version=1,
            plugin=dict(id="testing", name="Actions test", version=version),
            actions=dict(
                provider_key="default",
                catalog=dict(
                    actions_version=1,
                    categories=[],
                    menus=[],
                    actions=[
                        dict(
                            key="count",
                            title="Count",
                            input_schema=dict(
                                type="object", properties={}, additionalProperties=False
                            ),
                        )
                    ],
                ),
            ),
            runtime=dict(kind="process", executables={"linux-x86_64": "not-executed"}),
        )

    def provider(self, version, calls, *, register=True, key="count"):
        manifest = self.manifest(version)
        if register:
            self.api("POST", "/v1/plugins/manifest", manifest)
            self.api(
                "POST", f"/v1/plugins/testing@{version}/enabled", {"enabled": True}
            )
        provider = SloppyActionsProvider(
            plugin_id="testing",
            name="Actions test",
            descriptor=manifest["plugin"],
            connection_file=self.file,
            identity_directory=self.identity.directory,
        )
        provider.register_action(
            dict(key=key, title="Count"),
            lambda *_: calls.append(version) or {"status": "succeeded"},
        )
        self.providers.append(provider)
        provider.start()
        return provider

    def invoke(self, provider, *, ok=True):
        return self.api(
            "POST",
            f"/v1/actions/{provider.provider_id}/invoke",
            dict(
                binding_id=str(uuid.uuid4()),
                action_key="count",
                input={},
                execution_epoch=provider.execution_epoch,
                catalog_revision=provider.catalog_revision,
            ),
            ok=ok,
        )

    def test_two_versions_execute_independently_and_offline_clicks_are_not_replayed(
        self,
    ):
        calls = []
        one = self.provider("1.0.0", calls)
        two = self.provider("2.0.0", calls)
        one.ready.result(timeout=5)
        two.ready.result(timeout=5)
        self.assertNotEqual(one.provider_id, two.provider_id)
        self.assertIn("service_disabled", self.invoke(one, ok=False)["error"])
        self.assertEqual(calls, [])
        self.api("POST", "/v1/actions/enabled", {"enabled": True})
        first = self.invoke(one)
        wait_for(
            lambda: any(
                run["id"] == first["invocation_id"] and run["state"] == "succeeded"
                for run in self.api("GET", "/v1/actions/state")["runs"]
            )
        )
        self.invoke(two)
        wait_for(lambda: len(calls) == 2)
        original = one.provider_id
        one.close()
        wait_for(
            lambda: any(
                p["id"] == original and not p["online"]
                for p in self.api("GET", "/v1/actions/state")["providers"]
            )
        )
        self.assertIn("provider_unavailable", self.invoke(one, ok=False)["error"])
        replacement = self.provider("1.0.0", calls)
        self.assertEqual(replacement.ready.result(timeout=5), original)
        time.sleep(0.1)
        self.assertEqual(calls, ["1.0.0", "2.0.0"])

    def test_missing_version_is_rejected_with_guidance_and_does_not_install_itself(
        self,
    ):
        self.api("POST", "/v1/plugins/manifest", self.manifest("1.0.0"))
        provider = self.provider("2.0.0", [], register=False)
        with self.assertRaises(SloppyError) as error:
            provider.ready.result(timeout=5)
        self.assertEqual(error.exception.code, "plugin_version_mismatch")
        self.assertIn("testing@2.0.0", str(error.exception))
        self.assertIn("1.0.0", str(error.exception))
        wait_for(lambda: not provider._thread.is_alive())
        self.assertFalse(provider._thread.is_alive())
        plugins = self.api("GET", "/v1/plugins")
        self.assertFalse(
            any(
                item["plugin"]["id"] == "testing"
                and item["plugin"]["version"] == "2.0.0"
                for item in plugins
            )
        )
        self.assertEqual(self.api("GET", "/v1/actions/state")["providers"], [])

    def test_runtime_cannot_expand_the_surface_declared_in_the_installed_manifest(self):
        provider = self.provider("1.0.0", [], key="undeclared")
        with self.assertRaises(SloppyError) as error:
            provider.ready.result(timeout=5)
        self.assertIn("surface differs", str(error.exception))
        self.assertEqual(self.api("GET", "/v1/actions/state")["providers"], [])
