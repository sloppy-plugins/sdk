# Sloppy SDK

Public TypeScript and Python clients for Sloppy's session API. Both
implement TLS 1.3, certificate approval, owned sessions, reconnect/resume,
notifications, proposals, and authoritative completion. This repository is
self-contained and requires no Sloppy application source.

The canonical public repository is [sloppy-plugins/sdk](https://github.com/sloppy-plugins/sdk).

- [TypeScript API and examples](typescript/README.md)
- [Python API and examples](python/README.md)
- [Plugin setup, Git catalogue format, and catalogue authoring CLI](docs/plugins-and-catalogues.md)

Install published packages with `npm install @sloppy-plugins/sdk` or
`python -m pip install sloppy-sdk`. Package publishing is separate from building
this repository; the packages must exist on their public registries first.

## Connection errors

Both SDKs distinguish unavailable services from configuration and approval errors:

| Situation                                  | What to do                                                                                                                                              |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No connection file                         | Open Sloppy and enable its service server in Settings, or check a custom connection file path. A missing file alone does not prove the app is closed.   |
| Unreadable or malformed connection file    | Check permissions and the configured path; restart Sloppy to recreate its discovery file.                                                               |
| Service address cannot be reached          | Check that the server is running and its host/port are reachable. The SDK retries automatically.                                                        |
| Plugin version not installed               | Register/install the exact version in the target Sloppy instance and restart the plugin. `plugin_version_mismatch` ends the connection without retries. |
| Plugin integration disabled                | With the service running, run `sloppy plugins enable PLUGIN_ID@VERSION`.                                                                                |
| Client rejected or unknown clients ignored | Review the corresponding client approval or security setting in Sloppy, then restart the plugin.                                                        |
| New or changed service certificate         | Compare the fingerprint in Sloppy Settings and explicitly approve or replace it.                                                                        |

Error codes and retry behavior remain stable. User-facing messages explain the
state and next step; private diagnostics retain transport error codes without
logging credentials or bubble contents.

## Develop

Requirements: Node.js 22.23.4+ (22.x), 24.22.1+ (24.x), or 26.12.1+, npm, Python 3.11+, OpenSSL, and Make. On Windows,
use Make in a POSIX environment such as MSYS2 and set `PYTHON=python`.

```sh
make deps
make check
make pack
```

`make deps` uses the committed npm lockfile and creates `.venv` for Python tools.
It upgrades the bundled pip before installing SDK dependencies. Bootstrap pip
warnings use their normal handling; SDK installation and checks retain strict warnings.
`make build`, `make lint`, `make format`, `make format-check`, and `make test`
cover both SDKs. Focused targets are `build-typescript`, `build-python`,
`test-typescript`, and `test-python`. Filter Python tests with
`make test-python TEST_ARGS='-k pattern'`. `make clean` preserves dependencies.
Use `make lock` after changing Node dependencies and commit the updated lockfile.

Dependencies default to public npm and PyPI. Override `NPM_REGISTRY` and
`PYPI_INDEX` on the Make command line or in a gitignored `local.mk`; keep registry
credentials out of manifests and lockfiles. Compiler, linter, bundler, and Python
warnings fail checks.

Standalone fault and protocol tests require no server. Set
`SLOPPY_SDK_SERVICE_BINARY=/absolute/path/to/SloppyService` to include optional
integration tests against an externally supplied server. Those tests communicate
through the public API and do not import server implementation or fixtures.

## GitHub Actions

[`ci.yml`](.github/workflows/ci.yml) runs on pushes to `main`, pull requests, and
manual dispatch. It runs `make deps check pack` on Linux, macOS, and Windows with
the minimum Node.js/Python versions, plus Linux jobs covering Node.js 24 and 26
and Python 3.12–3.14. These standalone checks require no Sloppy source or server.

[`release.yml`](.github/workflows/release.yml) runs when a stable GitHub Release
is published. Its `vX.Y.Z` tag must match both public package versions and the
TypeScript version in `package-lock.json`. It checks and builds both SDKs before
separate npm and PyPI jobs publish the saved tarball, wheel, and source archive.
The npm package records `sloppy-plugins/sdk` as its source, including the
`typescript` subdirectory, for provenance. Release builds use fresh dependency
installs; the publishing jobs consume their checked artifacts without rebuilding.

Configure trusted publishing once in `github.com/sloppy-plugins/sdk`:

1. Create GitHub environments named `npm` and `pypi`. Configure any desired
   reviewer and release-tag rules there.
2. In the npm settings for `@sloppy-plugins/sdk`, add a GitHub Actions trusted
   publisher with owner `sloppy-plugins`, repository `sdk`, workflow filename `release.yml`,
   and environment `npm`. Allow direct `npm publish` for this workflow. A new npm package needs its first publication before
   a trusted publisher can be configured.
3. In the PyPI settings for `sloppy-sdk`, add a trusted publisher with the same
   repository and workflow filename, and environment `pypi`. For a new project,
   create a pending publisher instead.

Both publishing jobs use short-lived OIDC credentials rather than registry token
secrets. npm publishing includes provenance; the PyPA action also generates PyPI
attestations. See the [npm trusted publishing guide](https://docs.npmjs.com/trusted-publishers/)
and [PyPI trusted publishing guide](https://docs.pypi.org/trusted-publishers/).

For the first release, keep `0.1.0` and skip the version-bump commands. Bootstrap
the npm package with `make publish-npm` from the audited checkout before configuring
its trusted publisher; PyPI can use a pending publisher for its first upload.
The npm job skips an already published version only when its SHA-512 integrity
matches the checked tarball exactly. A different artifact at that version fails
without uploading or replacing anything.

To release:

```sh
make version-bump                 # Patch: 0.1.0 -> 0.1.1
make version-bump BUMP=minor      # Or a minor bump
make version-bump BUMP=major      # Or a major bump
make version-bump VERSION=1.2.3   # Or an explicit greater stable version
# Review and commit the change, then push/mirror it to github.com/sloppy-plugins/sdk.
make github-release-dry-run      # Run checks/packaging and preview locally
make github-release              # Create the stable GitHub Release and trigger publishing
```

Choose one version-bump command. It updates both SDKs, the repository development
workspace, and the npm lockfile together without tagging, committing, or pushing.
`github-release` runs `make check pack`, requires a clean committed checkout and
an authenticated GitHub CLI (`gh auth login --hostname github.com`), and verifies
that the exact local commit is already available in the canonical GitHub repo.
It creates `vX.Y.Z` at that commit with generated release notes. An existing tag
must point to the same commit. Releases always target the canonical GitHub
repository, independently of the checkout's configured origin. The dry-run target previews the command without contacting GitHub.

Prereleases do not publish packages. If one registry upload fails, rerun only
the failed jobs so the successful upload is not repeated. The workflows run in
the public GitHub SDK repository; the publishing destination and package metadata
are independent of the checkout's configured origin.

## Test other public clients against this source

Build this SDK first, then use the public example repositories' source override:

```sh
make deps build
make -C /path/to/copilot deps SDK_SOURCE="$PWD/typescript"
make -C /path/to/testing deps SDK_SOURCE="$PWD/python"
```

The examples use registry packages by default. Source overrides stay local to
dependency setup and do not replace their public dependency declarations.

## Publish

Use `make version-bump` to update the package versions together, then run
`make check pack`. The npm tarball is written to
`dist/`; Python source/wheel artifacts are written to `python/dist/`.

Publishing requires registry credentials and is always explicit:

```sh
make publish-npm
make publish-pypi
```

`make publish` runs both. Publishing targets run all checks and always use public
npm/PyPI, independently of dependency mirrors. No dependency, build, test, or
packaging target uploads artifacts.
