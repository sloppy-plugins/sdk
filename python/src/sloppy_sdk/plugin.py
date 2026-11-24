"""Standalone, standard-library plugin setup helper; also usable before SDK install."""

import argparse
import base64
import binascii
import json
import os
import re
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from urllib.parse import urlsplit


def validate_icon(value):
    message = (
        "Plugin icon must be a base64 PNG data URI, at most 64 KiB and 256×256 pixels"
    )
    if (
        not isinstance(value, str)
        or len(value) > 87406
        or not value.startswith("data:image/png;base64,")
    ):
        raise ValueError(message)
    try:
        data = base64.b64decode(value[22:], validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError(message) from error
    if (
        not 33 <= len(data) <= 65536
        or data[:16] != b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR"
        or not 1 <= int.from_bytes(data[16:20], "big") <= 256
        or not 1 <= int.from_bytes(data[20:24], "big") <= 256
    ):
        raise ValueError(message)


def set_icon(directory, image):
    root = Path(directory).resolve()
    manifest = inspect_manifest(root)
    manifest["plugin"]["icon"] = "data:image/png;base64," + base64.b64encode(
        Path(image).read_bytes()
    ).decode("ascii")
    validate_manifest(manifest)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=root, delete=False
    ) as output:
        temporary = Path(output.name)
        json.dump(manifest, output, indent=2, ensure_ascii=False)
        output.write("\n")
    try:
        os.replace(temporary, root / "plugin.json")
    finally:
        temporary.unlink(missing_ok=True)


def package_path(root, value):
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("Setup paths must be package-relative")
    target = (root / value).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Setup path escapes the copied plugin")
    return target


def relative_path(value):
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError(
            "Setup paths must be package-relative without parent traversal"
        )


def git_dependency(value):
    if not isinstance(value, dict) or set(value) != {
        "repository",
        "commit",
        "package_directory",
    }:
        raise ValueError("SDK sources require repository, commit and package_directory")
    url = urlsplit(value["repository"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not url.path.strip("/")
    ):
        raise ValueError("SDK repositories require HTTPS without embedded credentials")
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value["commit"]):
        raise ValueError("SDK sources must be pinned to a full Git commit hash")
    relative_path(value["package_directory"])
    return (
        "git+"
        + value["repository"]
        + "@"
        + value["commit"]
        + "#subdirectory="
        + value["package_directory"]
    )


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) - {
        "manifest_version",
        "protocol_version",
        "plugin",
        "runtime",
        "actions",
        "dependencies",
        "initialization",
        "background",
    }:
        raise ValueError("Unknown plugin manifest fields")
    if (
        manifest.get("manifest_version") != 1
        or type(manifest.get("protocol_version")) is not int
        or manifest["protocol_version"] < 1
    ):
        raise ValueError("Expected a version-1 plugin manifest with protocol_version")
    descriptor = manifest.get("plugin", {})
    if not isinstance(descriptor, dict):
        raise ValueError("Plugin descriptor must be an object")
    for field in ("id", "name", "version"):
        if not isinstance(descriptor.get(field), str) or not descriptor[field].strip():
            raise ValueError(f"Plugin {field} is required")
    if not re.fullmatch(r"[a-z0-9-]{1,64}", descriptor["id"]) or descriptor["id"] in (
        "plugins",
        "trust",
    ):
        raise ValueError("Invalid or reserved plugin ID")
    if "icon" in descriptor:
        validate_icon(descriptor["icon"])
    runtime = manifest.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("Plugin runtime must be an object")
    if (
        runtime.get("kind") != "process"
        or not isinstance(runtime.get("executables"), dict)
        or not runtime["executables"]
    ):
        raise ValueError("A process runtime with platform executables is required")
    for executable in runtime["executables"].values():
        relative_path(executable)
    dependencies = manifest.get("dependencies", [])
    if not isinstance(dependencies, list) or len(dependencies) > 8:
        raise ValueError("Invalid dependency list")
    seen = set()
    for dependency in dependencies:
        if (
            not isinstance(dependency, dict)
            or set(dependency) != {"runtime", "minimum_version"}
            or dependency["runtime"] not in ("python", "node", "git")
            or dependency["runtime"] in seen
            or not re.fullmatch(r"\d+(?:\.\d+){0,2}", dependency["minimum_version"])
        ):
            raise ValueError(
                "Dependencies require unique runtimes and numeric minimum versions"
            )
        seen.add(dependency["runtime"])
    steps = manifest.get("initialization", [])
    if not isinstance(steps, list) or len(steps) > 32:
        raise ValueError("Invalid initialization steps")
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Initialization steps must be objects")
        kind = step.get("kind")
        allowed = {
            "python_venv": {"kind", "directory", "requirements", "sdk"},
            "npm_install": {"kind", "directory", "sdk"},
            "command": {"kind", "program", "arguments", "directory", "if_missing"},
        }
        if kind not in allowed or set(step) - allowed[kind]:
            raise ValueError("Unknown initialization step or fields")
        relative_path(step.get("directory", ".venv" if kind == "python_venv" else "."))
        if kind == "python_venv":
            relative_path(step["requirements"])
            if "python" not in seen or step.get("directory", ".venv") == ".":
                raise ValueError(
                    "Python setup requires a declared Python runtime and its own environment directory"
                )
        if kind == "npm_install" and "node" not in seen:
            raise ValueError("npm setup requires a declared Node runtime")
        if step.get("sdk") is not None:
            git_dependency(step["sdk"])
            if "git" not in seen:
                raise ValueError("Git SDK sources require a declared Git dependency")
        if kind == "command":
            if (
                not isinstance(step.get("program"), str)
                or not step["program"]
                or any(c in step["program"] for c in "/\\")
                or not isinstance(step.get("arguments"), list)
                or any(
                    not isinstance(arg, str) or "\0" in arg for arg in step["arguments"]
                )
            ):
                raise ValueError(
                    "Commands need an executable name and an argument array"
                )
            if step.get("if_missing") is not None:
                relative_path(step["if_missing"])
    packaged = manifest.get("actions")
    if packaged is not None:
        if not isinstance(packaged, dict) or not isinstance(
            packaged.get("catalog"), dict
        ):
            raise ValueError("Action catalogue must be an object")
        catalog = packaged.get("catalog", {})
        if (
            not isinstance(packaged.get("provider_key"), str)
            or not packaged["provider_key"]
            or catalog.get("actions_version") != 1
            or not isinstance(catalog.get("actions"), list)
        ):
            raise ValueError("Invalid action catalogue declaration")
        keys = set()
        for action in catalog["actions"]:
            if not isinstance(action, dict):
                raise ValueError("Action declarations must be objects")
            key = action.get("key")
            if (
                not isinstance(key, str)
                or not key
                or key in keys
                or not isinstance(action.get("title"), str)
                or not action["title"].strip()
            ):
                raise ValueError("Actions need unique keys and titles")
            keys.add(key)
            if action.get("input_schema") != {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }:
                raise ValueError(
                    "This Actions version supports empty-object invocation inputs"
                )
            if (
                action.get("interaction", "immediate") == "input_bubble"
                and action.get("prompt", {}).get("type") != "questionnaire"
            ):
                raise ValueError(
                    "Input actions must declare their questionnaire prompt"
                )
    return manifest


def inspect_manifest(directory):
    root = Path(directory).resolve()
    manifest = validate_manifest(
        json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    )
    for step in manifest.get("initialization", []):
        package_path(root, step.get("directory", "."))
    return manifest


def progress(message):
    print(json.dumps({"type": "progress", "message": message}), flush=True)


def prepare_step(directory, index):
    root = Path(directory).resolve()
    manifest = inspect_manifest(root)
    step = manifest.get("initialization", [])[index]
    if step["kind"] != "python_venv":
        raise ValueError("This helper prepares python_venv steps")
    environment = package_path(root, step.get("directory", ".venv"))
    if environment == root:
        raise ValueError("The virtual environment needs its own directory")
    requirements = package_path(root, step["requirements"])
    if not requirements.is_file():
        raise ValueError("The declared requirements file is missing")
    progress("Creating the plugin's private Python environment…")
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    progress("Downloading and installing Python dependencies…")
    sdk = ["sloppy-sdk @ " + git_dependency(step["sdk"])] if step.get("sdk") else []
    if sdk:
        source = step["sdk"]
        progress(
            f"Downloading the pinned Sloppy SDK from {source['repository']} (commit {source['commit']})…"
        )
    env = dict(os.environ)
    for key in ("PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        env.pop(key, None)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--prefix",
            str(environment),
            *sdk,
            "-r",
            str(requirements),
        ],
        cwd=root,
        env=env,
        check=True,
    )
    progress("Python dependencies are ready.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["validate", "prepare", "set-icon"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--step", type=int, default=0)
    parser.add_argument("--icon", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "validate":
            print(json.dumps(inspect_manifest(args.directory), indent=2))
        elif args.operation == "set-icon":
            if args.icon is None:
                raise ValueError("set-icon requires --icon PATH.png")
            set_icon(args.directory, args.icon)
        else:
            prepare_step(args.directory, args.step)
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        subprocess.CalledProcessError,
    ) as error:
        print(
            json.dumps({"type": "error", "message": str(error)}),
            file=sys.stderr,
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
