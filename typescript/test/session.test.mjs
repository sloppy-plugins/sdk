import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { once } from 'node:events';
import { createServer } from 'node:https';
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { WebSocketServer } from 'ws';
import { SloppyIdentity, SloppySession } from '../dist/index.js';

async function waitFor(predicate) {
  const deadline = Date.now() + 3_000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, 'Timed out waiting for SDK state');
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

async function fixture(t, options = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'sloppy-sdk-'));
  const file = join(directory, 'connection.json');
  const serviceIdentity = new SloppyIdentity({
    application: 'test service',
    pluginId: 'service',
    directory: join(directory, 'server'),
  });
  const identity = await serviceIdentity.load();
  const https = createServer({
    cert: identity.certificate_pem,
    key: identity.private_key_pem,
    minVersion: 'TLSv1.3',
    maxVersion: 'TLSv1.3',
    requestCert: true,
    rejectUnauthorized: false,
  });
  const server = new WebSocketServer({ server: https });
  https.listen(0, '127.0.0.1');
  await once(https, 'listening');
  const connection = {
    protocol_version: 1,
    service: 'SloppyService',
    instance_id: randomUUID(),
    token: 'a'.repeat(64),
    base_url: `https://127.0.0.1:${server.address().port}`,
  };
  await writeFile(file, JSON.stringify(connection));
  const sessions = new Map();
  const requests = [];
  const hellos = [];
  let creates = 0;
  let dropped = false;
  server.on('connection', (socket, request) => {
    assert.ok(request.socket.getPeerCertificate().raw, 'Client did not present a certificate');
    assert.equal(request.headers.authorization, `Bearer ${connection.token}`);
    assert.equal(request.url, '/v1/sessions');
    let session;
    let heartbeat;
    socket.send(JSON.stringify({ type: 'trust_ready', protocol_version: 1 }));
    socket.on('close', () => clearInterval(heartbeat));
    socket.on('message', (raw) => {
      const body = JSON.parse(raw);
      if (body.type === 'hello') {
        hellos.push(body);
        if (body.resume && (options.denyResume || !sessions.has(body.resume.session_id))) {
          socket.send(
            JSON.stringify({
              type: 'error',
              code: options.denyResume ? 'session_denied' : 'session_unknown',
              message: 'Cannot resume',
            }),
          );
          return;
        }
        session = body.resume
          ? sessions.get(body.resume.session_id)
          : {
              id: randomUUID(),
              token: 'b'.repeat(64),
              items: new Map(),
              replies: new Map(),
            };
        sessions.set(session.id, session);
        session.socket = socket;
        const ready = {
          type: 'ready',
          protocol_version: 1,
          ...connection,
          session_id: session.id,
          resume_token: session.token,
          heartbeat_interval_ms: 30,
          heartbeat_timeout_ms: 150,
          pending_reply_ids: [...session.replies.keys()],
          notifications: [...session.items.values()],
          ...options.ready,
        };
        socket.send(JSON.stringify(ready));
        if (options.malformed) socket.send('not json');
        if (!options.silent)
          heartbeat = setInterval(() => {
            if (socket.readyState === 1) socket.send(JSON.stringify({ type: 'ping' }));
          }, 30);
        return;
      }
      if (body.type !== 'request') {
        if (body.type === 'received') session.replies.delete(body.request_id);
        return;
      }
      requests.push(body);
      let reply = session.replies.get(body.request_id);
      if (!reply) {
        let value;
        if (body.op === 'create') {
          creates++;
          value = {
            ...body.notification,
            id: body.id,
            session_id: session.id,
            state: 'pending',
            requires_response: body.notification.requires_response ?? false,
          };
          session.items.set(body.id, value);
          options.afterCreate?.(session, value);
        } else if (body.op === 'list') {
          value = [...session.items.values()];
        } else {
          value = session.items.get(body.id);
          if (body.op === 'answer' || body.op === 'resolve') {
            value = {
              ...value,
              state: body.op === 'answer' ? 'submitted' : 'resolved',
              resolution: { answer: body.answer },
            };
          } else if (body.op === 'dismiss') {
            value = { ...value, state: 'cancelled', cancellation: { reason: body.reason } };
          }
          session.items.set(body.id, value);
        }
        reply = {
          type: 'reply',
          request_id: body.request_id,
          ok: true,
          value: options.badReply ? null : value,
        };
        session.replies.set(body.request_id, reply);
      }
      if (options.dropAfterApply && !dropped) {
        dropped = true;
        socket.terminate();
      } else {
        socket.send(JSON.stringify(reply));
        if (body.op !== 'list')
          socket.send(
            JSON.stringify({
              type: 'notification',
              notification: session.items.get(body.id),
            }),
          );
      }
    });
  });
  const sdk = new SloppySession({
    pluginId: 'testing',
    connectionFile: file,
    identityDirectory: join(directory, 'client'),
    confirmServer: async (presented) =>
      presented.fingerprint === (await serviceIdentity.fingerprint()),
    retryDelayMs: 10,
    maxRetryDelayMs: 50,
  });
  t.after(async () => {
    sdk.close();
    for (const socket of server.clients) socket.terminate();
    await new Promise((resolve) => server.close(resolve));
    await new Promise((resolve) => https.close(resolve));
    await rm(directory, { recursive: true });
  });
  return {
    sdk,
    sessions,
    requests,
    hellos,
    connection,
    file,
    logConfig: join(directory, 'logs', 'verbosity.json'),
    get creates() {
      return creates;
    },
    async restart() {
      sessions.clear();
      connection.instance_id = randomUUID();
      await writeFile(file, JSON.stringify(connection));
      for (const socket of server.clients) socket.terminate();
    },
    drop() {
      for (const socket of server.clients) socket.terminate();
    },
  };
}

test('private session diagnostics record reconnects without bubble contents or credentials', async (t) => {
  const f = await fixture(t);
  await f.sdk.ready;
  const secret = 'DO_NOT_LOG_BUBBLE_TEXT';
  await f.sdk.send({ title: secret, message: secret, details: { secret } }).created;
  const sessionId = f.sdk.sessionId;
  f.drop();
  await waitFor(() => f.hellos.length >= 2 && f.sdk.online);
  assert.equal(f.sdk.sessionId, sessionId);
  f.sdk.close();
  const contents = await readFile(f.sdk.diagnosticFile, 'utf8');
  const lines = contents.trim().split('\n').map(JSON.parse);
  for (const event of ['connect_attempt', 'ready', 'closed'])
    assert.ok(
      lines.some((line) => line.event === event),
      event,
    );
  assert.ok(lines.some((line) => line.event === 'connect_attempt' && line.detail.resumed));
  assert.ok(lines.every((line) => line.level !== 'debug'));
  for (const value of [secret, f.connection.token, 'b'.repeat(64), 'resume_token'])
    assert.ok(!contents.includes(value), value);
  if (process.platform !== 'win32')
    assert.equal((await stat(f.sdk.diagnosticFile)).mode & 0o777, 0o600);
});

test('shared verbosity changes SDK recording without reconnecting', async (t) => {
  const f = await fixture(t);
  await f.sdk.ready;
  const bubble = await f.sdk.send({ title: 'Quiet default' }).created;
  let records = (await readFile(f.sdk.diagnosticFile, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.ok(records.every((line) => line.level === 'info'));
  await writeFile(f.logConfig, JSON.stringify({ level: 'debug' }));
  await f.sdk.get(bubble.id);
  records = (await readFile(f.sdk.diagnosticFile, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.ok(records.some((line) => line.event === 'sending' && line.level === 'debug'));
  assert.ok(records.some((line) => line.event === 'received' && line.level === 'debug'));
  await writeFile(f.logConfig, JSON.stringify({ level: 'error' }));
  const size = (await stat(f.sdk.diagnosticFile)).size;
  await f.sdk.get(bubble.id);
  assert.equal((await stat(f.sdk.diagnosticFile)).size, size);
  assert.equal(f.hellos.length, 1);
  await writeFile(f.logConfig, JSON.stringify({ level: ['debug'] }));
  await f.sdk.get(bubble.id);
  assert.equal(f.sdk.diagnosticError?.code, 'diagnostic_error');
  assert.equal((await stat(f.sdk.diagnosticFile)).size, size);
  await writeFile(f.logConfig, JSON.stringify({ level: 'info' }));
});

test('oversized retained snapshots report the exact transport limit in private diagnostics', async (t) => {
  const f = await fixture(t, {
    ready: {
      notifications: [
        {
          id: randomUUID(),
          state: 'pending',
          requires_response: false,
          message: 'x'.repeat(2 * 1024 * 1024),
        },
      ],
    },
  });
  await waitFor(() => f.hellos.length >= 2);
  const contents = await readFile(f.sdk.diagnosticFile, 'utf8');
  assert.ok(contents.includes('WS_ERR_UNSUPPORTED_MESSAGE_LENGTH'));
  assert.ok(!contents.includes('x'.repeat(100)));
  assert.equal(f.sdk.online, false);
});

test('diagnostics rotate at 1 MiB with one backup', async (t) => {
  const f = await fixture(t);
  await f.sdk.ready;
  const path = f.sdk.diagnosticFile;
  await writeFile(path, 'x'.repeat(1024 * 1024));
  f.sdk.close();
  assert.ok((await stat(path)).size < 1024 * 1024);
  assert.equal((await stat(path + '.1')).size, 1024 * 1024);
  assert.ok((await readFile(path, 'utf8')).includes('"event":"closed"'));
});

test('diagnostic write failures are visible without disrupting session recovery', async (t) => {
  const f = await fixture(t);
  await f.sdk.ready;
  const path = f.sdk.diagnosticFile;
  await rm(path);
  await mkdir(path);
  const failures = [];
  f.sdk.on('diagnostic_error', (error) => failures.push(error));
  f.drop();
  await waitFor(() => f.hellos.length >= 2 && f.sdk.online);
  assert.equal(f.sdk.diagnosticError?.code, 'diagnostic_error');
  assert.ok(failures.length > 0);
  assert.ok(failures.every((error) => error.message === 'Cannot write private Sloppy session log'));
});

test('missing discovery after Sloppy exits is a readable offline state and reconnects', async (t) => {
  const f = await fixture(t);
  await f.sdk.ready;
  const sessionId = f.sdk.sessionId;
  const statuses = [];
  f.sdk.on('status', (status) => statuses.push(status));
  await rm(f.file);
  f.drop();
  await waitFor(() =>
    statuses.some(
      (status) =>
        status.code === 'service_unavailable' &&
        status.error?.includes('Enable Sloppy service server'),
    ),
  );
  assert.equal(f.sdk.online, false);
  assert.equal(f.sdk.sessionId, sessionId);
  assert.ok(statuses.every((status) => !status.error?.includes('ENOENT')));
  await writeFile(f.file, JSON.stringify(f.connection));
  await waitFor(() => f.sdk.online);
  assert.equal(f.sdk.sessionId, sessionId);
  assert.equal(f.sdk.lastError, null);
});

test('responses are proposals; accepting does not resolve, and completion is explicit', async (t) => {
  const { sdk } = await fixture(t);
  await sdk.ready;
  const bubble = sdk.send({ title: 'Approval', requires_response: true });
  await bubble.created;
  const answer = { source: 'native', selected_choice_ids: ['yes'], value: true };
  const submitted = await sdk.answer(bubble.id, answer);
  assert.equal(submitted.state, 'submitted');
  assert.deepEqual(await bubble, { ...answer, source: 'testing' });
  assert.equal((await sdk.acknowledge(bubble.id, 'accepted')).state, 'submitted');
  assert.equal((await sdk.resolve(bubble.id, answer)).state, 'resolved');
});

test('informational bubbles resolve on creation; dismissals reject without an answer', async (t) => {
  const { sdk } = await fixture(t);
  const info = sdk.send({ title: 'Info' });
  assert.equal(await info, null);
  const bubble = sdk.send({ title: 'Question', requires_response: true });
  await bubble.created;
  await sdk.dismiss(bubble.id, 'Native close');
  await assert.rejects(bubble, { code: 'dismissed' });
});

test('dropped replies retry the same operation ID without duplicate bubbles', async (t) => {
  const f = await fixture(t, { dropAfterApply: true });
  const bubble = f.sdk.send({ title: 'Once' });
  await bubble.created;
  assert.equal(f.creates, 1);
  assert.equal(f.requests.length, 2);
  assert.equal(f.requests[0].request_id, f.requests[1].request_id);
  assert.equal(f.hellos[1].resume.session_id, f.sdk.sessionId);
});

test('answers missed during disconnect arrive in the resumed snapshot', async (t) => {
  const f = await fixture(t);
  const bubble = f.sdk.send({ title: 'Question', requires_response: true });
  await bubble.created;
  const owner = f.sessions.get(f.sdk.sessionId);
  const answer = { value: 'received while disconnected' };
  owner.items.set(bubble.id, {
    ...owner.items.get(bubble.id),
    state: 'submitted',
    resolution: { answer },
  });

  f.drop();
  assert.deepEqual(await bubble, answer);
  assert.equal(f.sdk.sessionId, owner.id);
});

test('a cached creation reply cannot overwrite a newer submitted notification', async (t) => {
  const answer = { value: 'Already submitted' };
  const f = await fixture(t, {
    dropAfterApply: true,
    afterCreate: (owner, item) => {
      owner.items.set(item.id, { ...item, state: 'submitted', resolution: { answer } });
    },
  });
  const states = [];
  f.sdk.on('notification', (item) => states.push(item.state));
  const bubble = f.sdk.send({ title: 'Question', requires_response: true });
  assert.deepEqual(await bubble, answer);
  await bubble.created;
  assert.ok(states.includes('submitted'));
  assert.ok(!states.slice(states.indexOf('submitted')).includes('pending'));
});

test('Sloppy restart retires old bubbles and creates a fresh session without replay', async (t) => {
  const f = await fixture(t);
  const bubble = f.sdk.send({ title: 'Old', requires_response: true });
  await bubble.created;
  const oldId = f.sdk.sessionId;
  const lost = assert.rejects(bubble, { code: 'session_lost' });
  await f.restart();
  await lost;
  await waitFor(() => f.sdk.online && f.sdk.sessionId !== oldId);
  await f.sdk.send({ title: 'New' });
  assert.equal(f.creates, 2);
});

test('forgotten sessions recover without reusing old bubbles', async (t) => {
  const f = await fixture(t);
  const bubble = f.sdk.send({ title: 'Old', requires_response: true });
  await bubble.created;
  const oldId = f.sdk.sessionId;
  const lost = assert.rejects(bubble, { code: 'session_lost' });
  f.sessions.clear();
  f.drop();
  await lost;
  await waitFor(() => f.sdk.online && f.sdk.sessionId !== oldId);
  assert.equal(f.creates, 1);
});

test('invalid credentials fail outstanding bubbles rather than silently retrying', async (t) => {
  const f = await fixture(t, { denyResume: true });
  const bubble = f.sdk.send({ title: 'Old', requires_response: true });
  await bubble.created;
  f.drop();
  await assert.rejects(bubble, { code: 'session_denied' });
  assert.equal(f.sdk.online, false);
  assert.match(f.sdk.lastError, /could not resume/);
  assert.match(f.sdk.lastError, /Restart this plugin/);
  assert.equal(f.hellos.length, 2);
});

test('heartbeat failures are reported and the SDK reconnects', async (t) => {
  const f = await fixture(t, { silent: true });
  const errors = [];
  f.sdk.on('status', (status) => {
    if (status.error) errors.push(status.error);
  });
  await f.sdk.ready;
  await waitFor(() => errors.some((error) => error.includes('heartbeat')));
  await waitFor(() => f.hellos.length > 1);
});

test('malformed frames and replies reject work explicitly', async (t) => {
  const f = await fixture(t, { badReply: true });
  const bubble = f.sdk.send({ title: 'Bad reply' });
  await assert.rejects(bubble.created, { code: 'protocol_error' });
  await assert.rejects(bubble, { code: 'protocol_error' });
});

test('version mismatch ends the session with installed-version guidance and no retry', async (t) => {
  const f = await fixture(t, {
    ready: {
      type: 'error',
      code: 'plugin_version_mismatch',
      message:
        'Plugin version mismatch: testing@2.0.0 is not installed. Installed versions: 1.0.0.',
    },
  });
  await assert.rejects(
    f.sdk.ready,
    (error) =>
      error.code === 'plugin_version_mismatch' &&
      /testing@2\.0\.0/.test(error.message) &&
      /1\.0\.0/.test(error.message),
  );
  const attempts = f.hellos.length;
  await new Promise((resolve) => setTimeout(resolve, 150));
  assert.equal(f.hellos.length, attempts);
  assert.equal(f.sdk.online, false);
});

test('malformed cancellation data rejects work without stranding a bubble', async (t) => {
  const f = await fixture(t, {
    afterCreate: (_owner, item) => {
      item.state = 'cancelled';
      item.cancellation = 17;
    },
  });
  const bubble = f.sdk.send({ title: 'Bad cancellation' });
  await assert.rejects(bubble.created, { code: 'protocol_error' });
  await assert.rejects(bubble, { code: 'protocol_error' });
});

test('invalid JSON frames stop the session with a visible protocol error', async (t) => {
  const f = await fixture(t, { malformed: true });
  const bubble = f.sdk.send({ title: 'Malformed frame' });
  await assert.rejects(bubble.created, { code: 'protocol_error' });
  assert.ok(f.sdk.lastError);
});

test('acknowledgements lost before reconnect do not fill the server reply cache', async (t) => {
  const f = await fixture(t);
  await f.sdk.send({ title: 'Old' });
  const owner = f.sessions.get(f.sdk.sessionId);
  const orphan = randomUUID();
  owner.replies.set(orphan, { type: 'reply', request_id: orphan, ok: true, value: null });
  f.drop();
  await waitFor(() => f.hellos.length > 1 && !owner.replies.has(orphan));
});

test('malformed handshake deadlines cannot leave requests hanging', async (t) => {
  const f = await fixture(t, { ready: { heartbeat_timeout_ms: -1 } });
  await assert.rejects(f.sdk.ready, { code: 'protocol_error' });
  assert.equal(f.sdk.online, false);
});

test('queued payloads are immutable snapshots and invalid JSON is rejected', async (t) => {
  const { sdk, requests } = await fixture(t);
  const content = { title: 'Original', details: { nested: 'original' } };
  const bubble = sdk.send(content);
  content.details.nested = 'mutated';
  await bubble.created;
  assert.equal(requests[0].notification.details.nested, 'original');
  const invalid = sdk.send({ details: { value: NaN } });
  await assert.rejects(invalid.created, { code: 'invalid_input' });
  await assert.rejects(invalid, { code: 'invalid_input' });
  const cycle = {};
  cycle.self = cycle;
  const circular = sdk.send({ details: cycle });
  await assert.rejects(circular.created, { code: 'invalid_input' });
  await assert.rejects(circular, { code: 'invalid_input' });
});

test('close rejects queued work and stops reconnecting', async (t) => {
  const { sdk } = await fixture(t);
  const bubble = sdk.send({ title: 'Queued' });
  sdk.close();
  await assert.rejects(bubble, { code: 'closed' });
  await assert.rejects(bubble.created, { code: 'closed' });
  await assert.rejects(sdk.ready, { code: 'closed' });
  await assert.rejects(sdk.list(), { code: 'closed' });
});
