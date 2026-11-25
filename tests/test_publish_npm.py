import base64
import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import Mock

SPEC = importlib.util.spec_from_file_location(
    "publish_npm", Path(__file__).resolve().parents[1] / "scripts/publish-npm.py"
)
npm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(npm)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.artifact = Path(self.temporary.name) / "sdk.tgz"
        data = json.dumps({"name": "@sloppy-plugins/sdk", "version": "0.1.0"}).encode()
        with tarfile.open(self.artifact, "w:gz") as archive:
            info = tarfile.TarInfo("package/package.json")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    def test_identical_existing_bootstrap_upload_is_skipped(self):
        integrity = (
            "sha512-"
            + base64.b64encode(
                hashlib.sha512(self.artifact.read_bytes()).digest()
            ).decode()
        )
        run = Mock(
            return_value=subprocess.CompletedProcess([], 0, json.dumps(integrity), "")
        )
        npm.publish(self.artifact, run)
        self.assertEqual(run.call_count, 1)
        self.assertIn("@sloppy-plugins/sdk@0.1.0", run.call_args.args[0])

    def test_existing_version_with_different_contents_is_never_overwritten(self):
        run = Mock(
            return_value=subprocess.CompletedProcess([], 0, '"sha512-other"', "")
        )
        with self.assertRaisesRegex(ValueError, "immutable"):
            npm.publish(self.artifact, run)
        self.assertEqual(run.call_count, 1)

    def test_missing_version_publishes_checked_artifact_with_provenance(self):
        run = Mock(
            side_effect=[
                subprocess.CompletedProcess([], 1, '{"error":{"code":"E404"}}', ""),
                subprocess.CompletedProcess([], 0),
            ]
        )
        npm.publish(self.artifact, run)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args.args[0],
            [
                "npm",
                "publish",
                str(self.artifact),
                "--access",
                "public",
                "--provenance",
                "--registry=https://registry.npmjs.org/",
            ],
        )

    def test_registry_or_authentication_failure_stops_before_upload(self):
        for response in ('{"error":{"code":"E403"}}', "", "not JSON"):
            with self.subTest(response=response):
                run = Mock(
                    return_value=subprocess.CompletedProcess(
                        [], 1, response, "registry unavailable"
                    )
                )
                with self.assertRaisesRegex(ValueError, "no upload was attempted"):
                    npm.publish(self.artifact, run)
                self.assertEqual(run.call_count, 1)

    def test_relative_tarball_is_passed_as_an_unambiguous_local_path(self):
        data = self.artifact.read_bytes()
        run = Mock(
            side_effect=[
                subprocess.CompletedProcess([], 1, '{"error":{"code":"E404"}}', ""),
                subprocess.CompletedProcess([], 0),
            ]
        )
        with chdir(self.temporary.name):
            artifact = Path("dist/sdk.tgz")
            artifact.parent.mkdir()
            artifact.write_bytes(data)
            npm.publish(artifact, run)
            expected = str(artifact.absolute())
        self.assertEqual(run.call_args.args[0][2], expected)
        self.assertTrue(Path(run.call_args.args[0][2]).is_absolute())


if __name__ == "__main__":
    unittest.main()
