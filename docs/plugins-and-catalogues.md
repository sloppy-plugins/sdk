# Installing plugins and maintaining catalogues

Sloppy copies plugins into its Application Support directory. A copied plugin is
independent of its source folder: editing an unpacked checkout does not change
installed code. In a sandboxed app this directory is inside the app container.
Sloppy's Plugins settings can browse Git catalogues, load an unpacked folder,
prepare dependencies with progress/cancellation, select older or newer versions,
and remove installations. Source-version launches must be stopped before a
version is replaced or removed.

The terminal launcher and Settings share installed versions and default choices.
An explicit replacement migrates compatible saved Actions. The review lists the
supported bindings that will move and unsupported bindings/placements that will
be removed. Replacing a version with an already installed version reuses that
installation. Other versions, unrelated providers, names, shortcuts, and supported
dot/menu placements are retained.

## Version-1 plugin manifests

Keep `manifest_version: 1`. A manifest contains `protocol_version`, `plugin`, and
a process `runtime`, plus optional `actions`, `dependencies`, `initialization`,
and `background`. The complete Actions catalogue and questionnaire prompts belong
in the manifest, so they can be inspected before installation or execution.

A Python plugin can declare:

```json
{
  "manifest_version": 1,
  "protocol_version": 1,
  "plugin": { "id": "example", "name": "Example", "version": "1.0.0" },
  "runtime": {
    "kind": "process",
    "executables": { "macos-aarch64": "example.py", "linux-x86_64": "example.py" }
  },
  "dependencies": [{ "runtime": "python", "minimum_version": "3.11" }],
  "initialization": [
    { "kind": "python_venv", "directory": ".venv", "requirements": "requirements.txt" }
  ],
  "background": { "arguments": ["--service"] }
}
```

`background` is optional. Declare it only for a process that can run without a
terminal. Sloppy offers Start/Stop for that mode. Interactive agent adapters keep
their originating application's or terminal's launch flow. Python environments
are created at their final installation path, so script shebangs remain valid.
Sloppy uses the prepared environment's Python when launching this plugin.

For a Node.js plugin, declare a Node runtime dependency and these setup steps:

```json
{
  "dependencies": [{ "runtime": "node", "minimum_version": "22.23.4" }],
  "initialization": [
    { "kind": "npm_install", "directory": "." },
    {
      "kind": "command",
      "program": "npm",
      "arguments": ["run", "build"],
      "directory": ".",
      "if_missing": "dist/launch.mjs"
    }
  ]
}
```

`npm_install` uses `npm ci` when a package lock exists and `npm install` otherwise.
The command step skips a build when its declared artifact already exists, which
lets the same manifest work in a source checkout and a packaged distribution.
Use an argument array; Sloppy adds no implicit shell. Commands run from the copied
package, and package-relative paths cannot traverse to a parent directory.
Declared commands and dependency install scripts execute with the user's account
permissions. Sloppy shows the source/setup before asking to prepare the plugin.

Supported runtime requirements are `python`, `node`, and `git`, with a numeric
`minimum_version`. Missing or older runtimes produce an inline message and the
official download link. Sloppy does not install those system runtimes itself.
Dependencies inside the private Python/Node environment are installed automatically.
Failure or cancellation leaves previously installed versions and Actions intact.

### SDKs from pinned Git commits

For a private or unreleased SDK, both setup steps accept an optional `sdk`:

```json
{
  "kind": "python_venv",
  "directory": ".venv",
  "requirements": "requirements.txt",
  "sdk": {
    "repository": "https://github.com/sloppy-plugins/sdk.git",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "package_directory": "python"
  }
}
```

Also declare a Git dependency, such as
`{"runtime":"git","minimum_version":"2.25"}`. Use a real full commit hash in
your manifest. Python installs that SDK alongside the requirements file. For
`npm_install`, set `package_directory` to `typescript`; the helper fetches/verifies
the SDK commit, installs/builds its workspace, then installs the plugin with that
local SDK. The source checkout stays inside the private installation. This avoids
requiring a registry release before a private catalogue can be tested.

Normal manifests can omit `sdk` and use published package versions. Git access
uses the user's configured Git credentials; HTTPS URLs cannot contain passwords
or tokens. Never put credentials into a manifest or catalogue.

### Setup commands and SDK environment

The SDK distributes standalone setup helpers used by Sloppy before plugin
dependencies have been installed:

```sh
# Python SDK installation
sloppy-plugin-setup validate /path/to/plugin
sloppy-plugin-setup prepare /path/to/plugin --step 0
# Or: python -m sloppy_sdk.plugin validate /path/to/plugin

# Node project with @sloppy-plugins/sdk installed
npx --no-install sloppy-plugin-setup validate /path/to/plugin
npx --no-install sloppy-plugin-setup prepare /path/to/plugin --step 0
```

The Python helper handles `python_venv`, and the Node helper handles `npm_install`.
`--step` indexes the manifest's complete initialization list. Helpers emit JSON
progress records for hosts and forward the package manager's normal output.
The catalogue commands below use readable status/error messages.

Sloppy supplies `SLOPPY_PLUGIN_ID`, `SLOPPY_PLUGIN_VERSION`,
`SLOPPY_PLUGIN_MANIFEST`, `SLOPPY_CONNECTION_FILE`, and
`SLOPPY_IDENTITY_PROFILE`, and `SLOPPY_IDENTITY_DIRECTORY`. The explicit identity
directory keeps managed launches paired with the installation that Sloppy trusted.
Managed background launches also get
`SLOPPY_PLUGIN_DATA_DIR` for writable state. Keep state out of installed code.
Both SDK sessions use the launcher identity profile by default; reuse it for a
paired action provider. A plugin must not silently change its manifest during
initialization. Sloppy checks it again before publishing the installation.

## Git catalogue format

A Git repository is a Sloppy catalogue when its root contains a valid
`catalogue.json`:

```json
{
  "catalogue_version": 1,
  "type": "sloppy.plugin-catalogue",
  "name": "Example Catalogue",
  "description": "Plugins maintained by Example",
  "plugins": [
    {
      "id": "example",
      "name": "Example",
      "repository": "https://github.com/example/sloppy-example.git",
      "versions": [
        {
          "version": "1.0.0",
          "commit": "0123456789abcdef0123456789abcdef01234567",
          "package_directory": ".",
          "manifest": {
            "manifest_version": 1,
            "protocol_version": 1,
            "plugin": { "id": "example", "name": "Example", "version": "1.0.0" },
            "runtime": {
              "kind": "process",
              "interpreter": "node",
              "executables": { "macos-aarch64": "example.mjs" }
            }
          }
        }
      ]
    }
  ]
}
```

Include the **entire** per-version manifest, including Actions, dependencies and
initialization. The advertised manifest must match the checked-out commit's
`plugin.json`. `package_directory` is relative to the repository root and defaults
to `.`. Git commits must be full 40- or 64-character hexadecimal hashes; branches,
tags, abbreviated hashes and traversal paths are rejected. Repository URLs use
HTTPS without credentials, query strings or fragments.

Versions are semantic versions. Browsing defaults to the highest stable version,
falling back to a prerelease only when no stable release exists. Older versions
remain selectable. Plugin IDs and each plugin's versions must be unique. An
existing release is immutable: create a new plugin version for a new release.
A refresh validates the entire catalogue before replacing its cached contents;
a failed refresh keeps the last valid snapshot and reports its error.

Each catalogue has its own source identity. If two catalogues provide the same
plugin ID, the user can see/select the catalogue and version explicitly. Updating
an installed catalogue plugin prefers its original source.

## Catalogue authoring CLI

Both SDK distributions provide **`sloppy-catalogue`** with the same commands.
Choose the installed Python CLI, `python -m sloppy_sdk.catalogue`, or the Node
project's `npx --no-install sloppy-catalogue`. Node authors need no Python to use
the Node command. These commands require no running Sloppy service.

```sh
sloppy-catalogue init catalogue.json --name "Example Catalogue"
sloppy-catalogue add-plugin catalogue.json --manifest ../example/plugin.json
sloppy-catalogue validate catalogue.json

# Commit a changed plugin version and code in its own repository first.
sloppy-catalogue add-version catalogue.json --manifest ../example/plugin.json

# Metadata can also be supplied explicitly.
sloppy-catalogue add-version catalogue.json \
  --manifest /path/to/release/plugin.json \
  --repository https://github.com/example/sloppy-example.git \
  --commit 0123456789abcdef0123456789abcdef01234567 \
  --package-directory .

sloppy-catalogue remove-version catalogue.json example 1.0.0
```

When omitted, repository/commit come from the manifest checkout's Git origin and
HEAD; the package directory is inferred from its location in that checkout.
The manifest must match the committed manifest; commit it before recording
the release. Use explicit metadata for a separately prepared release manifest.
`add-plugin` rejects an existing plugin; `add-version` requires an existing plugin
and rejects an existing version. Removing its last release removes that plugin's
entry. Invalid edits leave the previous file untouched. Writes use a temporary
file and atomic replacement. No command commits or pushes a Git repository.
Review `git diff`, commit, and publish through your normal repository workflow.

The SDK tools check catalogue structure, release identity/pins, and common manifest
fields. Sloppy also validates the complete typed Actions and bubble contracts
before installation. Test a catalogue refresh and an installation in Sloppy before
publishing a release.

### Plugin icons

`plugin.icon` is an optional `data:image/png;base64,…` string in the existing
version-1 manifest. Use a square PNG (128×128 recommended), at most 256×256 pixels
and 64 KiB decoded. Icons are embedded so catalogues can show artwork before
installation and disconnected clients do not need a network request. Remote URLs,
SVG markup and file paths are not accepted. Omitted icons use Sloppy's generic
plugin symbol.

Keep the original artwork and PNG in the plugin repository. Embed a PNG without
hand-editing base64:

```sh
sloppy-plugin-setup set-icon ./my-plugin --icon ./my-plugin/assets/icon.png
# Or with the Python SDK:
python -m sloppy_sdk.plugin set-icon ./my-plugin --icon ./my-plugin/assets/icon.png
# Or the standalone Node helper:
node path/to/sdk/typescript/plugin-setup.mjs set-icon ./my-plugin --icon ./my-plugin/assets/icon.png
```

Both helpers validate the image envelope and update `plugin.json` atomically.
Catalogue authoring preserves the entire descriptor, including artwork. Commit the
manifest with its artwork before adding a release to a catalogue. Existing
catalogue releases remain immutable; add a new plugin release to distribute new
artwork to installed plugins.
