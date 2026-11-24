# sloppy-sdk

Python SDK for isolated, resumable Sloppy bubble sessions. Requires Python 3.11+.
Its protocol, recovery behavior, and lifecycle match the TypeScript package.

## Respond to a bubble

```python
from sloppy_sdk import SloppySession

with SloppySession(plugin_id="my-plugin", application="my-application") as session:
    bubble = session.send(
        {
            "kind": "question",
            "title": "Choose a name",
            "message": "Enter a name for this operation.",
            "requires_response": True,
            "response": {"value_type": "text", "allow_custom": True, "choices": []},
        }
    )
    print("Created:", bubble.created.result().get("id"))
    answer = bubble.result()
    session.acknowledge(bubble.id, "accepted").result()
    # Perform the originating application's operation here.
    session.resolve(bubble.id, answer).result()
```

Pair certificates, then register the exact version with
`sloppy plugins register DIRECTORY` and enable it before sending. `descriptor`
supplies same-version session layout metadata; connecting cannot register or
replace a plugin version. `plugin_version` defaults to the descriptor version
or `SLOPPY_PLUGIN_VERSION`. Unknown versions fail with `plugin_version_mismatch`
and end the connection without automatic retries.
`plugin_id` defaults to `SLOPPY_PLUGIN_ID` when launched by Sloppy's CLI.

`send()` immediately returns a `BubbleFuture` with a stable `.id` and separate
`.created` future. Informational bubbles return `None` on creation. A dismissal
raises `SloppyError` with `code == "dismissed"` and submits no answer.

**An answer is a proposal, not successful execution.** Accepting it does not
resolve the bubble. Use `acknowledge(id, "accepted" | "rejected", message=None)`
for acceptance/rejection and `resolve(id, answer)` only after your application
actually completes the operation. A rejected proposal reopens native controls.

Every public future supports both `.result(timeout=...)` and `await`, including
`session.ready`, `bubble.created`, `list()`, and all mutation operations:

```python
async def inspect(session):
    await session.ready
    return await session.list()
```

Transport runs in its own thread. `on(event, callback)` handlers and future
callbacks run separately so they can wait for another SDK operation without
blocking heartbeats. Callback exceptions are logged. A `.result()` timeout or
cancelled await stops only the caller's wait; it does not dismiss the bubble or
retract an already-sent operation.

## Public API

| Method/property                                   | Purpose                                          |
| ------------------------------------------------- | ------------------------------------------------ |
| `ready`                                           | Future fulfilled after the first valid handshake |
| `online`, `last_error`                            | Current health and last failure                  |
| `session_id`, `instance_id`, `resume_credentials` | Identity and a copied resume capability          |
| `send(content)`                                   | Create a bubble and await its proposed answer    |
| `list()`, `get(id)`                               | Read this session's records only                 |
| `answer(id, answer)`                              | Submit an originating-app proposal               |
| `acknowledge(id, status, message=None)`           | Accept/reject without completing                 |
| `resolve(id, answer)`                             | Publish authoritative completion                 |
| `dismiss(id, reason=...)`                         | Cancel the mirror without answering              |
| `close()`                                         | Stop retrying and reject outstanding work        |

Events are `status` (`{"online": bool, "error": str | None}`), `ready` (credentials
after each handshake), `notification` (an owned record), and `session_lost` (an
error). Subscribe before waiting on `ready`. Use notifications to observe later
completion/rejection: the response future settles only once.

The SDK automatically writes private metadata-only JSON-lines diagnostics beside
the connection file at `logs/client-sessions/<session-id>.log`. Plugins do not
need their own logger. Before the first handshake the filename uses a local UUID;
`diagnostic_file` reports the current path. Files rotate at 1 MiB to one `.log.1`
backup. Unix directories are owner-only and files use mode `0600`. Connection
attempts, retries, resumes, timeout categories, transport/close codes, and frame
sizes/counts are recorded, never bubble content, answers, arbitrary error
messages, or credentials. No transport diagnostics go to stdout/stderr. Write
failures are exposed through `diagnostic_error` and the `diagnostic_error` event
without interrupting recovery.

Sloppy's Sessions **Log verbosity** selector controls recording and display for
the server and local SDKs through private `logs/verbosity.json`. **Info** is the
default and records lifecycle events, retries, resumes, and failures. **Error**
records failures only; **Debug** additionally records frame traffic metadata.
Changes take effect on the next event without reconnecting. Standalone SDK usage
without this file defaults to Info. Invalid or unreadable settings surface through
`diagnostic_error` rather than silently changing the level.

Options also include `descriptor`, `connection_file`, `resume`, `retry_delay`
(default 0.5 seconds), and `max_retry_delay` (default 30 seconds). Discovery
otherwise uses `SLOPPY_CONNECTION_FILE` or Sloppy's platform-specific
application-data path. Connections require HTTPS authorities and TLS 1.3, without
environment proxies. Requests are immutable JSON snapshots bounded to 2 MiB;
at most 128 operations may be outstanding.

## Certificates and explicit pairing

`session.identity` is a `SloppyIdentity`, privately generated on first use using
the packaged cryptography dependency, not an external OpenSSL command. Display
`session.identity.fingerprint()` in your client. Sloppy independently offers
**Accept and remember**, **Accept once** (only the current connection), and
**Reject**. Rejection persists until its fingerprint is removed in Sloppy Settings.

An unknown server raises `SloppyTrustRequiredError` from `ready`, with code
`server_trust_required` and `presented` containing its endpoint, SHA, certificate
PEM and unverified name. Pass a synchronous `confirm_server(presented)` callback
that obtains an explicit decision through your application; only an exact `True`
saves the pin. It runs on the transport thread: marshal GUI interactions to the
appropriate UI thread. For explicit preconfigured approval, independently verify
the SHA in Sloppy Settings and supply it as configuration:

```python
import os
from sloppy_sdk import SloppySession

expected_fingerprint = os.environ["SLOPPY_VERIFIED_SERVICE_SHA"]
with SloppySession(
    plugin_id="my-plugin",
    application="my-application",
    endpoint="https://sloppy-host:8787",
    confirm_server=lambda presented: presented.fingerprint == expected_fingerprint,
) as session:
    print(session.identity.fingerprint())
```

Omit `endpoint` for local discovery. A changed server always fails with
`server_identity_changed` and `known_fingerprint`, **without calling the callback**.
Do not treat this as a retryable outage. `inspect_service(endpoint)` probes
without sending application data. After independent verification,
`identity.remember_service(presented)` adds a new pin;
`identity.remember_service(presented, expected_old_fingerprint)` explicitly
replaces it. `list_known_services()` lists pins, and
`remove_known_service(endpoint, expected_fingerprint)` removes one. Updates
compare the current pin to prevent accidental replacement.

`session.regenerate_certificate()` closes that session and returns the new client
SHA. Start a new session and approve its new SHA in Sloppy. Standalone
`SloppyIdentity(application=..., plugin_id=..., directory=None, profile=None)`
also offers `regenerate()`; other processes sharing a profile retain their old
key until disconnected. Service certificate rotation requires explicit pin
replacement on every client.

`identity_directory` (absolute) / `identity_profile` select storage; defaults use
`SLOPPY_IDENTITY_PROFILE` or a stable application/plugin hash. Profiles contain
`identity.json` and `known_sloppy` below the platform Sloppy application data
directory's `identities/` (`sloppy` on Linux). Names are at most 64 UTF-8 bytes.
Keep keys private. Remove a crashed owner's empty `.security-lock` only after
confirming no owner is running. Fingerprints hash the entire DER certificate,
not its key. See the service pairing contract for format and migration details.

## Recovery and ownership

Heartbeats and exponential reconnects are automatic. Temporary outages retain
ownership, retry unconfirmed operations with the same IDs, and reconcile the
owned notification snapshot. After a Sloppy restart or forgotten session, the
SDK establishes a fresh session and raises `session_lost` for previously
attempted bubbles. It never replays old decisions.

Persist `resume_credentials` to reclaim bubbles from a new plugin process, and
pass them as `resume`. Treat the capability token as a secret: use a private
file, never log it, and never put it in source control. A relaunched application
must reconcile `list()` with its durable request state; futures are not
persisted. Invalid credentials and malformed protocol messages fail explicitly.

Closing the session or dismissing its mirror cannot retract an operation already
submitted to the originating application. Use the context manager or call
`close()` explicitly to release transport and callback resources.

## Development

From the standalone workspace, run `make deps`, `make test`, and `make check`.
Fault tests use independent fake WebSocket servers. Set
`SLOPPY_SDK_SERVICE_BINARY` to an absolute `SloppyService` executable path for
real-service integration. `make pack` builds wheel/source packages without
uploading them.


## Plugin actions

`SloppyActionsProvider` publishes a static catalog independently of a bubble
session. A host must already have the exact plugin version and the identical
catalog registered from its version-1 `plugin.json`. Include all definitions,
categories, and questionnaire prompts under `actions.catalog`; use
`actions.provider_key` for the stable provider instance key. Registration is an
explicit administrator operation (`sloppy plugins register DIRECTORY`), never a
side effect of connecting.

```python
import json
from pathlib import Path
from sloppy_sdk import SloppyActionsProvider

manifest = json.loads(Path("plugin.json").read_text())
packaged = manifest["actions"]
provider = SloppyActionsProvider(
    plugin_id=manifest["plugin"]["id"],
    name=manifest["plugin"]["name"],
    descriptor=manifest["plugin"],
    provider_key=packaged["provider_key"],
    categories=packaged["catalog"].get("categories", []),
)


def run(context, values):
    context.check_cancelled()
    # Perform the declared action here.
    return {"status": "succeeded"}


for definition in packaged["catalog"]["actions"]:
    provider.register_action(definition, run)
provider.start()
provider.ready.result(timeout=30)
# Keep the process alive while publishing actions; call provider.close() on exit.
```

This first slice accepts empty-object invocation inputs only. Declare
`interaction: "input_bubble"` and a questionnaire `prompt` in the manifest for
runtime questions. The handler uses a `SloppySession` to show that exact prompt,
waits for the user's submitted proposal, acknowledges/resolves it, then executes.
Use the same identity directory as the session when publishing a paired provider.
Check `context.check_cancelled()` while waiting and return
`{"status": "cancelled"}` if the user dismisses the question. Handlers must
cooperate with cancellation; the SDK cannot undo external side effects.

Each execution has a deadline, immutable invocation UUID, and process epoch.
Reconnect resends unacknowledged terminal results, never handler execution.
`online` and `last_error` expose connection status. The `ready` future reports
fatal admission errors, including `plugin_version_mismatch`; these stop retries.
Register handlers before `start()`. Host actions must also be enabled in Service
settings. Bubble mirroring and action execution have separate enablement.

Saved bindings include a stable provider UUID scoped to approved identity,
plugin ID, version, and provider key. Keep credentials stable across restarts;
different versions must use separate identity profiles/directories. SDK sessions
send `plugin_version` from their descriptor, explicit argument, or launcher
environment. A host never redirects a binding to another version automatically.


## Plugin setup and catalogues

The SDK includes `sloppy-plugin-setup` and `sloppy-catalogue`. Use them to prepare
private environments, validate manifests, and add plugins or versions to a Git
catalogue. See [the setup and catalogue guide](https://github.com/sloppy-plugins/sdk/blob/main/docs/plugins-and-catalogues.md)
for the manifest fields, pinned Git SDK sources, matching Python/Node CLI commands,
atomic catalogue edits, and release workflow.
