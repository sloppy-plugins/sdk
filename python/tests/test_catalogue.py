import base64
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

from sloppy_sdk.catalogue import add_release, read, remove_release, validate, write
from sloppy_sdk.plugin import inspect_manifest, set_icon, validate_icon


def manifest(version="1.0.0"):
    return dict(
        manifest_version=1,
        protocol_version=1,
        plugin=dict(id="example", name="Example", version=version),
        runtime=dict(kind="process", executables={"linux-x86_64": "example.py"}),
    )


def empty():
    return dict(
        catalogue_version=1, type="sloppy.plugin-catalogue", name="Examples", plugins=[]
    )


class CatalogueTests(unittest.TestCase):
    def test_icons_survive_catalogues_and_invalid_or_remote_images_are_rejected(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg=="
        )
        uri = "data:image/png;base64," + base64.b64encode(png).decode()
        validate_icon(uri)
        source = manifest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin.json").write_text(json.dumps(source))
            (root / "icon.png").write_bytes(png)
            set_icon(root, root / "icon.png")
            source = inspect_manifest(root)
            self.assertEqual(source["plugin"]["icon"], uri)
        catalogue = add_release(
            empty(),
            source,
            "https://example.com/plugins/example",
            "a" * 40,
            new_plugin=True,
        )
        self.assertEqual(
            catalogue["plugins"][0]["versions"][0]["manifest"]["plugin"]["icon"], uri
        )
        for invalid in (
            "https://example.com/icon.png",
            "data:image/svg+xml;base64,AAAA",
            "data:image/png;base64,AA==",
            uri + "!",
            "data:image/png;base64," + "A" * 87400,
        ):
            with self.assertRaises(ValueError):
                validate_icon(invalid)
        data = bytearray(png)
        data[16:20] = (257).to_bytes(4, "big")
        with self.assertRaises(ValueError):
            validate_icon("data:image/png;base64," + base64.b64encode(data).decode())

    def test_plugin_and_version_addition_are_immutable_and_removal_is_explicit(self):
        source = empty()
        one = add_release(
            source,
            manifest(),
            "https://example.com/plugins/example",
            "a" * 40,
            new_plugin=True,
        )
        two = add_release(
            one, manifest("2.0.0"), "https://example.com/plugins/example.git", "b" * 40
        )
        self.assertEqual(source, empty())
        self.assertEqual(len(two["plugins"][0]["versions"]), 2)
        self.assertEqual(
            two["plugins"][0]["versions"][1]["manifest"], manifest("2.0.0")
        )
        with self.assertRaisesRegex(ValueError, "immutable"):
            add_release(
                two, manifest(), "https://example.com/plugins/example", "c" * 40
            )
        self.assertEqual(
            remove_release(remove_release(two, "example", "1.0.0"), "example", "2.0.0"),
            empty(),
        )

    def test_mismatched_manifests_links_commits_paths_and_unknown_fields_are_rejected(
        self,
    ):
        valid = add_release(
            empty(),
            manifest(),
            "https://example.com/plugins/example",
            "a" * 40,
            new_plugin=True,
        )
        for mutate in [
            lambda c: c.update(typo=True),
            lambda c: c["plugins"][0]["versions"][0].update(version="1.0.0-01"),
            lambda c: c["plugins"][0].update(
                repository="https://user:secret@example.com/repo"
            ),
            lambda c: c["plugins"][0]["versions"][0].update(commit="HEAD"),
            lambda c: c["plugins"][0]["versions"][0].update(
                package_directory="../outside"
            ),
            lambda c: c["plugins"][0]["versions"][0]["manifest"]["plugin"].update(
                version="2.0.0"
            ),
        ]:
            value = deepcopy(valid)
            mutate(value)
            with self.assertRaises(ValueError):
                validate(value)

    def test_invalid_edit_does_not_replace_catalogue_or_follow_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "catalogue.json"
            write(file, empty())
            before = file.read_bytes()
            broken = dict(empty(), catalogue_version=2)
            with self.assertRaises(ValueError):
                write(file, broken)
            self.assertEqual(file.read_bytes(), before)
            self.assertEqual(read(file), empty())
            folder = Path(temporary) / "plugin"
            folder.mkdir()
            (folder / "plugin.json").write_text(json.dumps(manifest()))
            self.assertEqual(inspect_manifest(folder), manifest())

    def test_cli_initializes_and_updates_a_catalogue_without_git_or_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "catalogue.json"
            plugin = Path(temporary) / "plugin.json"
            plugin.write_text(json.dumps(manifest()))
            for args in [
                ["init", str(file), "--name", "Examples"],
                [
                    "add-plugin",
                    str(file),
                    "--manifest",
                    str(plugin),
                    "--repository",
                    "https://example.com/plugins/example",
                    "--commit",
                    "a" * 40,
                ],
                ["validate", str(file)],
                ["remove-version", str(file), "example", "1.0.0"],
            ]:
                result = subprocess.run(
                    [sys.executable, "-m", "sloppy_sdk.catalogue", *args],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(read(file)["plugins"], [])

    def test_cli_infers_a_nested_package_and_rejects_an_uncommitted_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "packages" / "example"
            plugin.mkdir(parents=True)
            file = root / "catalogue.json"
            descriptor = plugin / "plugin.json"
            descriptor.write_text(json.dumps(manifest()))

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(root), *args], stderr=subprocess.PIPE
                )

            git("init")
            git("remote", "add", "origin", "https://example.com/plugins/example.git")
            git("add", "packages")
            git(
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "Fixture",
            )
            write(file, empty())
            args = [
                sys.executable,
                "-m",
                "sloppy_sdk.catalogue",
                "add-plugin",
                str(file),
                "--manifest",
                str(descriptor),
            ]
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            release = read(file)["plugins"][0]["versions"][0]
            self.assertEqual(release["package_directory"], "packages/example")
            self.assertEqual(
                release["commit"], git("rev-parse", "HEAD").decode().strip()
            )
            before = file.read_bytes()
            descriptor.write_text(json.dumps(manifest("2.0.0")))
            args[3] = "add-version"
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Commit the plugin manifest first", result.stderr)
            self.assertEqual(file.read_bytes(), before)


class SetupTests(unittest.TestCase):
    def test_private_python_environment_is_created_at_its_final_location(self):
        import os
        from unittest.mock import patch

        from sloppy_sdk.plugin import prepare_step

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = manifest()
            value["dependencies"] = [dict(runtime="python", minimum_version="3.11")]
            value["initialization"] = [
                dict(
                    kind="python_venv",
                    directory=".venv",
                    requirements="requirements.txt",
                )
            ]
            (root / "plugin.json").write_text(json.dumps(value))
            (root / "requirements.txt").write_text("")
            with patch.dict(os.environ, {"PIP_NO_INDEX": "1"}):
                prepare_step(root, 0)
            executable = (
                root
                / ".venv"
                / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            )
            result = subprocess.run(
                [executable, "-c", "import sys; print(sys.prefix)"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(
                Path(result.stdout.strip()).resolve(), (root / ".venv").resolve()
            )

    def test_pinned_git_sdk_installs_without_a_registry_release(self):
        import os
        from unittest.mock import patch

        from sloppy_sdk.plugin import prepare_step

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "sdk"
            source = repository / "python"
            source.mkdir(parents=True)
            (source / "pyproject.toml").write_text(
                '[build-system]\nrequires=[]\nbuild-backend="backend"\nbackend-path=["."]\n'
            )
            backend = """import zipfile
from pathlib import Path

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name="sloppy_sdk-0.1.0-py3-none-any.whl"
    files={
      "sloppy_sdk/__init__.py": 'SDK_READY = "pinned SDK"\\n',
      "sloppy_sdk-0.1.0.dist-info/METADATA": "Metadata-Version: 2.1\\nName: sloppy-sdk\\nVersion: 0.1.0\\n",
      "sloppy_sdk-0.1.0.dist-info/WHEEL": "Wheel-Version: 1.0\\nGenerator: fixture\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
    }
    files["sloppy_sdk-0.1.0.dist-info/RECORD"]="".join(path+",,\\n" for path in files)
    with zipfile.ZipFile(Path(wheel_directory)/name,"w") as wheel:
        for path, contents in files.items(): wheel.writestr(path,contents)
    return name
"""
            (source / "backend.py").write_text(backend)

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(repository), *args],
                    text=True,
                    stderr=subprocess.PIPE,
                ).strip()

            git("init")
            git("add", ".")
            git(
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "Fixture",
            )
            commit = git("rev-parse", "HEAD")
            plugin = root / "plugin"
            plugin.mkdir()
            value = manifest()
            value["dependencies"] = [
                dict(runtime="python", minimum_version="3.11"),
                dict(runtime="git", minimum_version="2.25"),
            ]
            value["initialization"] = [
                dict(
                    kind="python_venv",
                    requirements="requirements.txt",
                    sdk=dict(
                        repository="https://fixture.invalid/sdk.git",
                        commit=commit,
                        package_directory="python",
                    ),
                )
            ]
            (plugin / "plugin.json").write_text(json.dumps(value))
            (plugin / "requirements.txt").write_text("")
            env = {
                "PIP_NO_INDEX": "1",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url." + repository.as_uri() + ".insteadOf",
                "GIT_CONFIG_VALUE_0": "https://fixture.invalid/sdk.git",
                "GIT_ALLOW_PROTOCOL": "file",
            }
            output = io.StringIO()
            with patch.dict(os.environ, env), redirect_stdout(output):
                prepare_step(plugin, 0)
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertTrue(
                any(
                    event["type"] == "progress"
                    and value["initialization"][0]["sdk"]["repository"]
                    in event["message"]
                    and commit in event["message"]
                    for event in events
                ),
                "Download progress identifies the repository and pinned commit",
            )
            executable = (
                plugin
                / ".venv"
                / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            )
            imported = subprocess.run(
                [
                    executable,
                    "-c",
                    "from sloppy_sdk import SDK_READY; print(SDK_READY)",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(imported.stdout.strip(), "pinned SDK")
