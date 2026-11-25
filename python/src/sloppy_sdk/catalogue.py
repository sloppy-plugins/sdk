"""Maintain Sloppy plugin catalogues with Git and registry releases without publishing them."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from .plugin import validate_manifest

SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def repository_url(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not url.path.strip("/")
        or any(ord(c) < 33 for c in value)
    ):
        raise ValueError(
            "Repositories require HTTPS URLs without embedded credentials, queries or fragments"
        )
    return value.rstrip("/").removesuffix(".git") + ".git"


def relative_path(value):
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 for char in value)
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise ValueError("Package directory must be relative without parent traversal")


def validate_package_source(source):
    if not isinstance(source, dict):
        raise ValueError("Package sources must be objects")
    kind = source.get("kind")
    key = "package" if kind == "npm" else "project" if kind == "pypi" else None
    if key is None or set(source) != {"kind", key, "url", "sha256", "format"}:
        raise ValueError(
            "Expected an npm or PyPI source with URL, SHA-256, and archive format"
        )
    name = source[key]
    pattern = (
        r"(?:@[a-z0-9_.-]+/)?[a-z0-9][a-z0-9_.-]*"
        if kind == "npm"
        else r"[A-Za-z0-9][A-Za-z0-9_.-]*"
    )
    if not isinstance(name, str) or not re.fullmatch(pattern, name):
        raise ValueError("Invalid registry package name")
    repository_url(source["url"])
    if not isinstance(source["sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", source["sha256"]
    ):
        raise ValueError("Package releases require a lowercase SHA-256 digest")
    if source["format"] not in (("tar.gz",) if kind == "npm" else ("zip", "tar.gz")):
        raise ValueError("Unsupported package archive format")
    return source


def validate(catalogue):
    if set(catalogue) - {"catalogue_version", "type", "name", "description", "plugins"}:
        raise ValueError("Unknown catalogue fields")
    if (
        catalogue.get("catalogue_version") != 1
        or catalogue.get("type") != "sloppy.plugin-catalogue"
    ):
        raise ValueError("Expected a version-1 Sloppy plugin catalogue")
    if not isinstance(catalogue.get("name"), str) or not catalogue["name"].strip():
        raise ValueError("Catalogue name is required")
    plugins = catalogue.get("plugins")
    if not isinstance(plugins, list) or len(plugins) > 512:
        raise ValueError("Catalogue plugins must be a list of at most 512 entries")
    seen = set()
    for plugin in plugins:
        if set(plugin) - {"id", "name", "description", "repository", "versions"}:
            raise ValueError("Unknown plugin fields")
        if (
            not re.fullmatch(r"[a-z0-9-]{1,64}", plugin.get("id", ""))
            or plugin["id"] in seen
        ):
            raise ValueError("Plugin IDs must be valid and unique")
        seen.add(plugin["id"])
        if not isinstance(plugin.get("name"), str) or not plugin["name"].strip():
            raise ValueError("Plugin name is required")
        repository_url(plugin["repository"])
        versions = plugin.get("versions")
        if not isinstance(versions, list) or not 1 <= len(versions) <= 256:
            raise ValueError("Each plugin needs 1–256 releases")
        seen_versions = set()
        for release in versions:
            if set(release) - {
                "version",
                "commit",
                "package_directory",
                "manifest",
                "source",
            }:
                raise ValueError("Unknown release fields")
            version = release.get("version", "")
            if not SEMVER.fullmatch(version) or version in seen_versions:
                raise ValueError("Release versions must be unique semantic versions")
            seen_versions.add(version)
            if release.get("source") is not None:
                validate_package_source(release["source"])
                if release.get("commit") not in (None, ""):
                    raise ValueError(
                        "A release cannot specify both Git and registry sources"
                    )
            elif not re.fullmatch(
                r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", release.get("commit", "")
            ):
                raise ValueError("Each Git release needs a full Git commit hash")
            relative_path(release.get("package_directory", "."))
            manifest = release.get("manifest", {})
            validate_manifest(manifest)
            descriptor = manifest.get("plugin", {})
            if (
                manifest.get("manifest_version") != 1
                or manifest.get("protocol_version", 0) < 1
            ):
                raise ValueError(
                    "Each release needs a version-1 manifest with protocol_version"
                )
            if (
                descriptor.get("id") != plugin["id"]
                or descriptor.get("version") != version
            ):
                raise ValueError(
                    "Catalogue identity/version disagrees with its manifest"
                )
    return catalogue


def read(file):
    data = Path(file).read_bytes()
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Catalogue exceeds the 8 MB limit")
    return validate(json.loads(data))


def write(file, catalogue):
    validate(catalogue)
    file = Path(file)
    if file.is_symlink():
        raise ValueError("Refusing to replace a symbolic-link catalogue")
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n"
    if len(payload.encode()) > 8 * 1024 * 1024:
        raise ValueError("Catalogue exceeds the 8 MB limit")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=file.parent, delete=False
    ) as temporary:
        path = Path(temporary.name)
        try:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            path.unlink()
            raise
    try:
        os.replace(path, file)
    finally:
        path.unlink(missing_ok=True)


def add_release(
    catalogue, manifest, repository, commit, package_directory=".", *, new_plugin=False
):
    result = deepcopy(catalogue)
    descriptor = manifest["plugin"]
    plugin = next((p for p in result["plugins"] if p["id"] == descriptor["id"]), None)
    if new_plugin and plugin is not None:
        raise ValueError("Plugin already exists; use add-version")
    if not new_plugin and plugin is None:
        raise ValueError("Plugin is missing; use add-plugin")
    repository = repository_url(repository)
    if plugin is None:
        plugin = dict(
            id=descriptor["id"],
            name=descriptor["name"],
            description=descriptor.get("description", ""),
            repository=repository,
            versions=[],
        )
        result["plugins"].append(plugin)
    elif repository_url(plugin["repository"]) != repository:
        raise ValueError("The plugin repository differs from the existing entry")
    if any(r["version"] == descriptor["version"] for r in plugin["versions"]):
        raise ValueError(
            "This version already exists; published versions are immutable"
        )
    plugin["versions"].append(
        dict(
            version=descriptor["version"],
            commit=commit.lower(),
            package_directory=package_directory,
            manifest=deepcopy(manifest),
        )
    )
    return validate(result)


def remove_release(catalogue, plugin_id, version):
    result = deepcopy(catalogue)
    plugin = next((p for p in result["plugins"] if p["id"] == plugin_id), None)
    if plugin is None or not any(r["version"] == version for r in plugin["versions"]):
        raise ValueError("That plugin/version is not in the catalogue")
    plugin["versions"] = [r for r in plugin["versions"] if r["version"] != version]
    if not plugin["versions"]:
        result["plugins"].remove(plugin)
    return validate(result)


def git(directory, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(directory), *arguments],
        encoding="utf-8",
        stderr=subprocess.PIPE,
    ).rstrip("\r\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    init = commands.add_parser("init")
    init.add_argument("file", type=Path)
    init.add_argument("--name", required=True)
    init.add_argument("--description", default="")
    check = commands.add_parser("validate")
    check.add_argument("file", type=Path)
    for operation in ("add-plugin", "add-version"):
        command = commands.add_parser(operation)
        command.add_argument("file", type=Path)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--repository")
        command.add_argument("--commit")
        command.add_argument("--package-directory")
    remove = commands.add_parser("remove-version")
    remove.add_argument("file", type=Path)
    remove.add_argument("plugin_id")
    remove.add_argument("version")
    args = parser.parse_args()
    try:
        if args.operation == "init":
            if args.file.exists():
                raise ValueError("Catalogue already exists; it was not overwritten")
            write(
                args.file,
                dict(
                    catalogue_version=1,
                    type="sloppy.plugin-catalogue",
                    name=args.name,
                    description=args.description,
                    plugins=[],
                ),
            )
        elif args.operation == "validate":
            catalogue = read(args.file)
            print(
                f"Valid Sloppy catalogue: {catalogue['name']} ({len(catalogue['plugins'])} plugins)"
            )
            return 0
        elif args.operation == "remove-version":
            write(
                args.file, remove_release(read(args.file), args.plugin_id, args.version)
            )
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            directory = args.manifest.resolve().parent
            repository = args.repository or git(
                directory, "remote", "get-url", "origin"
            )
            commit = args.commit or git(directory, "rev-parse", "HEAD")
            if not args.commit:
                prefix = git(directory, "rev-parse", "--show-prefix")
                relative = prefix + args.manifest.resolve().name
                committed = json.loads(git(directory, "show", f"{commit}:{relative}"))
                if manifest != committed:
                    raise ValueError(
                        "Commit the plugin manifest first, or explicitly provide the intended --commit"
                    )
            package_directory = args.package_directory
            if package_directory is None:
                if args.commit:
                    package_directory = "."
                else:
                    package_directory = prefix.rstrip("/") or "."
            write(
                args.file,
                add_release(
                    read(args.file),
                    manifest,
                    repository,
                    commit,
                    package_directory,
                    new_plugin=args.operation == "add-plugin",
                ),
            )
        print(f"Updated {args.file}; no Git commit or push was performed.")
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"Catalogue update failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
