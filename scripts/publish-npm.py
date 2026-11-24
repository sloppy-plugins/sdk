"""Publish the checked SDK tarball, or recognize an identical npm bootstrap upload."""

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

REGISTRY = "https://registry.npmjs.org/"


def publish(artifact, run=subprocess.run):
    with tarfile.open(artifact, "r:gz") as archive:
        stream = archive.extractfile("package/package.json")
        if stream is None:
            raise ValueError("The npm tarball has no package manifest")
        with stream:
            package = json.load(stream)
    if not isinstance(package, dict):
        raise TypeError("The npm package manifest must be an object")
    name, version = package.get("name"), package.get("version")
    if (
        name != "@sloppy-plugins/sdk"
        or not isinstance(version, str)
        or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version)
    ):
        raise ValueError("Expected a stable @sloppy-plugins/sdk tarball")
    result = run(
        [
            "npm",
            "view",
            f"{name}@{version}",
            "dist.integrity",
            "--json",
            f"--registry={REGISTRY}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        existing = json.loads(result.stdout)
        digest = base64.b64encode(
            hashlib.sha512(artifact.read_bytes()).digest()
        ).decode()
        if existing != "sha512-" + digest:
            raise ValueError(
                f"{name}@{version} already exists with different contents; published versions are immutable"
            )
        print(
            f"{name}@{version} already contains this exact tarball; skipping npm upload."
        )
        return
    try:
        missing = json.loads(result.stdout).get("error", {}).get("code") == "E404"
    except (ValueError, AttributeError):
        missing = False
    if not missing:
        raise ValueError(
            "Could not check the public npm registry; no upload was attempted: "
            + result.stderr.strip()
        )
    run(
        [
            "npm",
            "publish",
            str(artifact),
            "--access",
            "public",
            "--provenance",
            f"--registry={REGISTRY}",
        ],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    try:
        publish(args.artifact)
    except (
        OSError,
        ValueError,
        KeyError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as error:
        print(f"npm publication failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
