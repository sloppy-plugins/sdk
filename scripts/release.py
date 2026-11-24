"""Keep SDK versions aligned and create releases in the canonical GitHub repo."""

import argparse
import json
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "github.com/sloppy-plugins/sdk"
API = "repos/sloppy-plugins/sdk"
STABLE = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


def version_numbers(version):
    if not isinstance(version, str) or not re.fullmatch(STABLE, version):
        raise ValueError(f"Expected a stable X.Y.Z version, received {version!r}")
    return tuple(map(int, version.split(".")))


def release_version(root=ROOT, tag=None):
    npm = json.loads((root / "typescript/package.json").read_text(encoding="utf-8"))[
        "version"
    ]
    pypi = tomllib.loads((root / "python/pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    locked = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))[
        "packages"
    ]["typescript"]["version"]
    version_numbers(npm)
    if not npm == pypi == locked:
        raise ValueError(f"Version mismatch: npm={npm}, PyPI={pypi}, lockfile={locked}")
    if tag is not None and tag != f"v{npm}":
        raise ValueError(f"Release tag must be v{npm}, received {tag!r}")
    return npm


def bump_version(root=ROOT, part="patch", version=None):
    current = release_version(root)
    previous = version_numbers(current)
    if version is None:
        numbers = list(previous)
        index = ("major", "minor", "patch").index(part)
        numbers[index] += 1
        numbers[index + 1 :] = [0] * (2 - index)
        version = ".".join(map(str, numbers))
    if version_numbers(version) <= previous:
        raise ValueError(f"New version {version} must be greater than {current}")

    contents = {}
    for name in ("package.json", "typescript/package.json", "package-lock.json"):
        path = root / name
        data = json.loads(path.read_text(encoding="utf-8"))
        data["version"] = version
        if name == "package-lock.json":
            data["packages"][""]["version"] = version
            data["packages"]["typescript"]["version"] = version
        contents[path] = json.dumps(data, indent=2) + "\n"
    path = root / "python/pyproject.toml"
    text, count = re.subn(
        r'(?ms)(^\[project\][ \t]*\n.*?^version[ \t]*=[ \t]*")[^"]+(\")',
        lambda match: match[1] + version + match[2],
        path.read_text(encoding="utf-8"),
        count=1,
    )
    if count != 1:
        raise ValueError('Expected a version = "X.Y.Z" field in [project]')
    contents[path] = text
    # Prepare every replacement before changing any files; preserve other metadata.
    for path, text in contents.items():
        path.write_text(text, encoding="utf-8")
    return version


def run(command, root, context):
    try:
        return subprocess.run(
            command, cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
    except FileNotFoundError as error:
        raise RuntimeError(f"Install {command[0]} before releasing") from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"{context}: {error.stderr.strip() or error.stdout.strip()}"
        ) from error


def github_release(root=ROOT, dry_run=False):
    version = release_version(root)
    if run(["git", "status", "--porcelain"], root, "Cannot inspect the SDK checkout"):
        raise ValueError("Commit the SDK changes before creating a GitHub Release")
    head = run(["git", "rev-parse", "HEAD"], root, "Cannot find the SDK commit")
    tag = f"v{version}"
    command = [
        "gh",
        "release",
        "create",
        tag,
        "--repo",
        REPOSITORY,
        "--target",
        head,
        "--title",
        tag,
        "--generate-notes",
    ]
    if dry_run:
        print(shlex.join(command))
        return

    run(
        ["gh", "auth", "status", "--hostname", "github.com"],
        root,
        "Authenticate using gh auth login --hostname github.com",
    )

    def api(path):
        return json.loads(
            run(
                ["gh", "api", "--hostname", "github.com", f"{API}/{path}"],
                root,
                f"Cannot read GitHub {path}; check authentication and push/mirror the SDK commit to https://{REPOSITORY}",
            )
        )

    api(f"commits/{head}")
    references = api(f"git/matching-refs/tags/{tag}")
    for reference in references:
        if reference["ref"] != f"refs/tags/{tag}":
            continue
        target = reference["object"]
        while target["type"] == "tag":
            target = api(f"git/tags/{target['sha']}")["object"]
        if target["type"] != "commit" or target["sha"] != head:
            raise ValueError(
                f"GitHub tag {tag} points to a different commit; choose a new version"
            )
    print(run(command, root, "Cannot create the GitHub Release"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-tag")
    check.add_argument("tag")
    bump = commands.add_parser("bump")
    bump.add_argument("--part", choices=("patch", "minor", "major"), default="patch")
    bump.add_argument("--version")
    release = commands.add_parser("github")
    release.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "check-tag":
            print(f"Releasing both SDKs at {release_version(tag=args.tag)}")
        elif args.command == "bump":
            print(
                f"Updated both SDKs to {bump_version(part=args.part, version=args.version)}"
            )
        else:
            github_release(dry_run=args.dry_run)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
