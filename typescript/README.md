# @sloppy-plugins/sdk

Node.js/TypeScript SDK for isolated, resumable Sloppy bubble sessions. Requires
Node.js 22.23.4+ (22.x), 24.22.1+ (24.x), or 26.12.1+. The same security
minimums apply to the workspace development tools.

## Respond to a bubble

```ts
import { SloppySession } from '@sloppy-plugins/sdk';

const session = new SloppySession({
  pluginId: 'my-plugin',
  application: 'my-application',
});

try {
  await session.ready;
  const bubble = session.send({
    kind: 'question',
    title: 'Choose a name',
    message: 'Enter a name for this operation.',
    requires_response: true,
    response: { value_type: 'text', allow_custom: true, choices: [] },
  });
  console.log('Created:', (await bubble.created).id);
  const answer = await bubble;
  await session.acknowledge(bubble.id, 'accepted');
  // Perform the originating application's operation here.
  await session.resolve(bubble.id, answer!);
} finally {
  session.close();
}
```

Pair certificates, then register the exact plugin version with
`sloppy plugins register DIRECTORY` and enable it before sending requests.
The `descriptor` supplies same-version session layout metadata; connecting cannot
register or replace a plugin version. `pluginVersion` defaults to the descriptor
version or `SLOPPY_PLUGIN_VERSION`; `pluginId` defaults to `SLOPPY_PLUGIN_ID`.
Unknown versions fail with `plugin_version_mismatch`, including the requested
and installed versions. This ends the connection without automatic retries.

`send()` immediately returns a promise with a stable `.id` and a separate
`.created` promise. `bubble.then(onAnswer, onError)` works without an intermediate
await. Informational bubbles resolve to `null` on creation. A dismissal rejects
with `SloppyError.code === 'dismissed'` and submits no answer.

**An answer is a proposal, not successful execution.** Accepting it does not
resolve the bubble. Use `acknowledge(id, 'accepted' | 'rejected', message?)` to
report acceptance/rejection and `resolve(id, answer)` only after your application
actually completes the request. A rejected proposal reopens its native controls.

## Public API

| Method/property                                | Purpose                                           |
| ---------------------------------------------- | ------------------------------------------------- |
| `ready`                                        | Promise fulfilled after the first valid handshake |
| `online`, `lastError`                          | Current transport health and last failure         |
| `sessionId`, `instanceId`, `resumeCredentials` | Identity and a copy of the resume capability      |
| `send(content)`                                | Create a bubble and await its proposed answer     |
| `list()`, `get(id)`                            | Read this session's records only                  |
| `answer(id, answer)`                           | Submit an originating-app proposal                |
| `acknowledge(id, status, message?)`            | Accept/reject a proposal without completing it    |
| `resolve(id, answer)`                          | Publish authoritative completion                  |
| `dismiss(id, reason?)`                         | Cancel the mirror without answering               |
| `close()`                                      | Stop retrying and reject outstanding work         |

Events are `status` (`{online, error, code?}`), `ready` (resume credentials after each
handshake), `notification` (a changed owned record), and `session_lost` (an error).
Expected temporary service unavailability uses `code: "service_unavailable"`;
plugins can handle that state without writing diagnostics over their host's UI.
Subscribe before awaiting `ready`. Event listeners should not throw exceptions.
Use notifications to observe later completion/rejection: the answer promise
settles only once.

Private JSON-lines diagnostics are automatic; nothing is written to stdout or
stderr. SDK logs live beside the connection file in
`logs/client-sessions/<session-id>.log`; service logs use
`logs/sessions/<session-id>.log`. Before the first handshake, the SDK uses a
local UUID filename. `diagnosticFile` reports the current filename. Each file
rotates at 1 MiB to one `.log.1` backup. On Unix, directories are owner-only
and files have mode `0600`. Events include connection attempts, retries,
resumes, timeout categories, WebSocket error/close codes, and frame sizes/counts.
They never include notification bodies, answers, descriptors, credentials,
arbitrary error messages, or WebSocket close reasons. Write failures are exposed
through `diagnosticError` and the `diagnostic_error` event without interrupting
session recovery.

Adapters can reuse the exported `SessionLog` to record `adapter_error` events
at Error verbosity. The Copilot adapter records only its component, operation,
and an allowlisted error code in the same private files; it never prints adapter
diagnostics over the host application's UI.

The service also maintains `logs/server.log`, a recent aggregate across sessions.
Sloppy's Sessions window displays this aggregate and separate server/SDK log
excerpts for each local session. SDK client files are not read for remote sessions.
The **Log verbosity** selector controls both recording and display. **Info** is
the default: connection lifecycle, retries, resumes, and failures, but no routine
frame traffic. **Error** records only failures; **Debug** additionally records
frame sizes/counts. The SDK reads the private `logs/verbosity.json` setting on each
event, so a change takes effect without reconnecting. Standalone SDK usage without
this file also defaults to Info. Invalid or unreadable settings are reported via
`diagnosticError`, never silently substituted.

Options also include `descriptor`, `connectionFile`, `resume`, `retryDelayMs`
(default 500), and `maxRetryDelayMs` (default 30,000). Discovery otherwise uses
`SLOPPY_CONNECTION_FILE` or Sloppy's platform-specific application-data path.
The SDK accepts only HTTPS authorities, uses TLS 1.3, and never follows
redirects. Requests are immutable JSON snapshots, bounded to 2 MiB, with at most
128 outstanding operations.

## Certificates and explicit pairing

`session.identity` is a `SloppyIdentity`; its certificate is created privately on
first use. It requires no OpenSSL executable. Use `await identity.fingerprint()`
to display its SHA in your application. Sloppy independently offers **Accept and
remember**, **Accept once** (only the current connection), and **Reject** for this
client. Rejected fingerprints persist until removed in Sloppy Settings.

Unknown servers reject `ready` with `SloppyTrustRequiredError`
(`code: 'server_trust_required'`, `presented` contains endpoint, fingerprint,
certificate PEM and an unverified name). Supply `confirmServer` to present these
details through your own explicit confirmation UI; it may return a boolean or a
promise. Only an exact `true` persists the pin. For example, a developer can
explicitly configure a fingerprint independently verified in Sloppy Settings:

```ts
const expectedFingerprint = process.env.SLOPPY_VERIFIED_SERVICE_SHA;
if (!expectedFingerprint) throw new Error('Verify and configure the Sloppy SHA first');
const session = new SloppySession({
  pluginId: 'my-plugin',
  application: 'my-application',
  endpoint: 'https://sloppy-host:8787',
  confirmServer: (presented) => presented.fingerprint === expectedFingerprint,
});
```

Omit `endpoint` for protected local discovery. Changed server pins always fail
with `server_identity_changed` and `knownFingerprint`, **without invoking the
callback**. Handle this as an explicit repair, not an availability retry.
`inspectService(endpoint)` probes without sending application data. After
independent verification, `identity.rememberService(presented)` creates a new
pin; `identity.rememberService(presented, expectedOldFingerprint)` explicitly
replaces it. `listKnownServices()` lists pins, and
`removeKnownService(endpoint, expectedFingerprint)` forgets one. These updates
compare the expected current fingerprint to prevent accidental replacement.

`await session.regenerateCertificate()` closes that session and returns the new
client SHA. Create a new session and approve its new SHA in Sloppy. Standalone
`SloppyIdentity` accepts `{ application, pluginId, directory?, profile? }` and
offers `regenerate()`; other processes sharing a profile retain their loaded
key until disconnected. Server certificate regeneration requires explicit pin
replacement in every client.

Options `identityDirectory` (absolute) and `identityProfile` select storage;
otherwise `SLOPPY_IDENTITY_PROFILE` or a stable hash of application/plugin identity
is used. Profiles contain `identity.json` and `known_sloppy` under
`Sloppy/identities/` in platform application data (`sloppy` on Linux).
Names are at most 64 UTF-8 bytes. Keep keys private. A crashed owner's empty
`.security-lock` may be removed only after confirming that owner is no longer
running. Certificate fingerprints hash the whole DER certificate, not its key.
See the service's pairing contract for format and migration details.

## Recovery and ownership

Heartbeat handling and exponential reconnects are automatic. Temporary outages
retain the session capability, retry unconfirmed operations with the same IDs,
and reconcile the owned notification snapshot. A restarted Sloppy or forgotten
session establishes a fresh session: previously attempted bubbles reject with
`session_lost`; old decisions are never replayed.

Persist `resumeCredentials` if a new plugin process needs to reclaim its existing
bubbles, then pass them as `resume`. Treat its token as a secret: use a private
file, never log it, and never put it in source control. A relaunched application
must reconcile `list()` with its own durable request state; JavaScript promises
are not persisted. Invalid credentials and malformed protocol messages fail
explicitly rather than retrying forever.

Closing a session, dismissing a mirror, or cancelling the caller's wait cannot
retract an operation already submitted to the originating application.

## Development

From the standalone workspace, run `make deps`, `make test`, and `make check`.
Fault tests use isolated fake WebSocket servers, not Sloppy internals. Set
`SLOPPY_SDK_SERVICE_BINARY` to an absolute `SloppyService` executable path to
include the real-service integration test. `make pack` creates publishable
artifacts without uploading them.

## Plugin setup and catalogues

The SDK includes `sloppy-plugin-setup` and `sloppy-catalogue`. Use them to prepare
private environments, validate manifests, and add plugins or versions to a Git
catalogue. See [the setup and catalogue guide](https://github.com/sloppy-plugins/sdk/blob/main/docs/plugins-and-catalogues.md)
for the manifest fields, pinned Git SDK sources, matching Python/Node CLI commands,
atomic catalogue edits, and release workflow.
