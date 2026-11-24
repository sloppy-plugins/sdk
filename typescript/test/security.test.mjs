import assert from 'node:assert/strict';
import { randomUUID, X509Certificate } from 'node:crypto';
import { once } from 'node:events';
import { mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { createServer } from 'node:https';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { WebSocketServer } from 'ws';
import {
  SloppyIdentity,
  SloppySession,
  SloppyTrustRequiredError,
  certificateFingerprint,
  inspectService,
} from '../dist/index.js';

async function fixture(t, mode = 'ready') {
  const directory = await mkdtemp(join(tmpdir(), 'sloppy-security-'));
  const serviceIdentity = new SloppyIdentity({
    application: 'Test Sloppy',
    pluginId: 'service',
    directory: join(directory, 'server'),
  });
  const service = await serviceIdentity.load();
  const https = createServer({
    cert: service.certificate_pem,
    key: service.private_key_pem,
    requestCert: true,
    rejectUnauthorized: false,
    minVersion: 'TLSv1.3',
    maxVersion: 'TLSv1.3',
  });
  const server = new WebSocketServer({ server: https });
  https.listen(0, '127.0.0.1');
  await once(https, 'listening');
  const endpoint = `https://127.0.0.1:${https.address().port}`;
  const sockets = [];
  const hellos = [];
  const fingerprints = [];
  const sessions = [];
  const send = (socket, value) => socket.send(JSON.stringify(value));
  server.on('connection', (socket, request) => {
    sockets.push(socket);
    fingerprints.push(certificateFingerprint(request.socket.getPeerCertificate().raw));
    if (mode === 'ready') send(socket, { type: 'trust_ready', protocol_version: 1 });
    else if (mode === 'pending') send(socket, { type: 'trust_pending', timeout_ms: 120000 });
    else send(socket, { type: 'error', code: mode, message: 'Client was denied' });
    socket.on('message', (bytes) => {
      const body = JSON.parse(bytes);
      hellos.push(body);
      if (body.type === 'hello')
        send(socket, {
          type: 'ready',
          protocol_version: 1,
          instance_id: randomUUID(),
          session_id: randomUUID(),
          resume_token: 'a'.repeat(64),
          heartbeat_interval_ms: 5000,
          heartbeat_timeout_ms: 15000,
          notifications: [],
          pending_reply_ids: [],
        });
    });
  });
  const options = {
    pluginId: 'testing',
    endpoint,
    identityDirectory: join(directory, 'client'),
    retryDelayMs: 10,
  };
  t.after(async () => {
    for (const session of sessions) session.close();
    for (const socket of server.clients) socket.terminate();
    await new Promise((resolve) => server.close(resolve));
    await new Promise((resolve) => https.close(resolve));
    await rm(directory, { recursive: true });
  });
  return {
    directory,
    endpoint,
    sockets,
    hellos,
    fingerprints,
    serviceIdentity,
    session(extra = {}) {
      const session = new SloppySession({ ...options, ...extra });
      sessions.push(session);
      return session;
    },
    approve() {
      for (const socket of sockets) send(socket, { type: 'trust_ready', protocol_version: 1 });
    },
    async rotate() {
      await serviceIdentity.regenerate();
      const next = await serviceIdentity.load();
      https.setSecureContext({ cert: next.certificate_pem, key: next.private_key_pem });
    },
  };
}

for (const [code, guidance] of [
  ['client_rejected', 'Rejected clients'],
  ['unknown_client_ignored', 'Ignore unknown clients'],
  ['client_trust_busy', 'pending approvals'],
  ['plugin_disabled', 'sloppy plugins enable testing'],
]) {
  test(`service status ${code} explains the actual state and recovery`, async (t) => {
    const f = await fixture(t, code);
    const session = f.session({ confirmServer: () => true });
    await waitFor(() => session.lastError !== null);
    assert.ok(session.lastError.includes(guidance), session.lastError);
    assert.ok(!session.lastError.includes('Client was denied'));
    assert.equal(session.online, false);
    assert.equal(f.hellos.length, 0);
  });
}

async function waitFor(predicate) {
  const deadline = Date.now() + 3000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, 'Timed out waiting for certificate negotiation');
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

test('certificate common names enforce a 64-byte limit', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'sloppy-name-'));
  t.after(() => rm(directory, { recursive: true }));
  const options = { pluginId: 'test', directory };
  assert.ok(
    (
      await new SloppyIdentity({ ...options, application: 'x'.repeat(64) }).fingerprint()
    ).startsWith('SHA256:'),
  );
  for (const application of ['x'.repeat(65), '\u00e9'.repeat(33), 'bad\nname'])
    await assert.rejects(new SloppyIdentity({ ...options, application }).regenerate(), {
      code: 'invalid_input',
    });
});

test('new service requires explicit approval before transmitting client identity or application headers', async (t) => {
  const f = await fixture(t);
  const session = f.session();
  await assert.rejects(session.ready, (error) => {
    assert.ok(error instanceof SloppyTrustRequiredError);
    assert.equal(error.code, 'server_trust_required');
    assert.equal(error.knownFingerprint, undefined);
    assert.equal(error.presented.endpoint, f.endpoint);
    return true;
  });
  assert.equal(f.sockets.length, 0);
  assert.deepEqual(await session.identity.listKnownServices(), []);
  assert.ok((await session.identity.fingerprint()).startsWith('SHA256:'));
});

test('client approval gates hello and queued requests while the SDK waits', async (t) => {
  const f = await fixture(t, 'pending');
  let confirmed = false;
  const session = f.session({
    confirmServer: async (service) => {
      confirmed = service.fingerprint === (await f.serviceIdentity.fingerprint());
      return confirmed;
    },
  });
  await waitFor(() => f.sockets.length === 1);
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(confirmed, true);
  assert.equal(session.online, false);
  assert.equal(f.hellos.length, 0);
  f.approve();
  await session.ready;
  assert.equal(f.hellos[0].type, 'hello');
  assert.equal(f.fingerprints[0], await session.identity.fingerprint());
});

test('remembered server skips prompts; changed certificates hard-fail until explicit replacement', async (t) => {
  const f = await fixture(t);
  const first = f.session({ confirmServer: () => true });
  await first.ready;
  const old = (await first.identity.listKnownServices())[0];
  first.close();
  const remembered = f.session({ confirmServer: () => assert.fail('Remembered service prompted') });
  await remembered.ready;
  remembered.close();
  await f.rotate();
  const changed = f.session({
    confirmServer: () => assert.fail('Changed service must hard-fail, not re-prompt'),
  });
  let challenge;
  await assert.rejects(changed.ready, (error) => {
    assert.equal(error.code, 'server_identity_changed');
    assert.equal(error.knownFingerprint, old.fingerprint);
    challenge = error;
    return true;
  });
  await assert.rejects(changed.identity.rememberService(challenge.presented), {
    code: 'server_identity_changed',
  });
  await changed.identity.rememberService(challenge.presented, old.fingerprint);
  const replaced = f.session();
  await replaced.ready;
  assert.equal(
    (await replaced.identity.listKnownServices())[0].fingerprint,
    await f.serviceIdentity.fingerprint(),
  );
});

test('rotation between approval and upgrade cannot leak bearer headers or a hello', async (t) => {
  const f = await fixture(t);
  const session = f.session({
    confirmServer: async () => {
      await f.rotate();
      return true;
    },
  });
  await assert.rejects(session.ready, { code: 'server_identity_changed' });
  assert.equal(f.sockets.length, 0);
  assert.equal(f.hellos.length, 0);
});

for (const code of ['client_rejected', 'unknown_client_ignored', 'client_trust_busy']) {
  test(`${code} is fatal and does not cause repeated approval requests`, async (t) => {
    const f = await fixture(t, code);
    const session = f.session({ confirmServer: () => true });
    await assert.rejects(session.ready, { code });
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.equal(f.sockets.length, 1);
    assert.equal(f.hellos.length, 0);
  });
}

test('client rotation closes its session, persists a new identity, and preserves known services', async (t) => {
  const f = await fixture(t);
  const first = f.session({ confirmServer: () => true });
  await first.ready;
  const old = await first.identity.fingerprint();
  const next = await first.regenerateCertificate();
  assert.notEqual(next, old);
  assert.equal(first.online, false);
  const session = f.session();
  await session.ready;
  assert.equal(f.fingerprints.at(-1), next);
  assert.equal((await session.identity.listKnownServices()).length, 1);
});

test('identity and known-service files are private, corruption is fatal, and removal is compare-and-swap', async (t) => {
  const f = await fixture(t);
  const session = f.session({ confirmServer: () => true });
  await session.ready;
  const known = (await session.identity.listKnownServices())[0];
  await assert.rejects(session.identity.removeKnownService(f.endpoint, 'wrong'), {
    code: 'server_identity_changed',
  });
  await session.identity.removeKnownService(f.endpoint, known.fingerprint);
  assert.deepEqual(await session.identity.listKnownServices(), []);
  if (process.platform !== 'win32') {
    for (const file of ['identity.json', 'known_sloppy'])
      assert.equal((await stat(join(session.identity.directory, file))).mode & 0o777, 0o600);
  }
  const original = await readFile(join(session.identity.directory, 'identity.json'), 'utf8');
  await writeFile(join(session.identity.directory, 'identity.json'), 'null');
  await assert.rejects(session.identity.fingerprint(), { code: 'identity_error' });
  await writeFile(
    join(session.identity.directory, 'identity.json'),
    JSON.stringify({
      ...JSON.parse(original),
      private_key_pem: 'not a private key',
    }),
  );
  await assert.rejects(session.identity.fingerprint(), { code: 'identity_error' });
  await writeFile(join(session.identity.directory, 'identity.json'), original);
  const identity = JSON.parse(original);
  assert.equal(
    certificateFingerprint(new X509Certificate(identity.certificate_pem).raw),
    await session.identity.fingerprint(),
  );
  assert.equal(
    (await inspectService(f.endpoint)).fingerprint,
    await f.serviceIdentity.fingerprint(),
  );
  await writeFile(
    join(session.identity.directory, 'known_sloppy'),
    JSON.stringify({
      version: 1,
      services: { [f.endpoint]: { ...known, certificate_pem: 'not a certificate' } },
    }),
  );
  await assert.rejects(session.identity.listKnownServices(), { code: 'identity_error' });
});

test('concurrent automatic generation reuses the same per-profile identity', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'sloppy-concurrent-'));
  t.after(() => rm(directory, { recursive: true }));
  const identities = Array.from(
    { length: 8 },
    () => new SloppyIdentity({ application: 'test', pluginId: 'test', directory }),
  );
  const fingerprints = await Promise.all(identities.map((identity) => identity.fingerprint()));
  assert.ok(fingerprints.every((fingerprint) => fingerprint === fingerprints[0]));
});

test('managed identity directory is shared and an explicit directory wins', () => {
  const previous = process.env.SLOPPY_IDENTITY_DIRECTORY;
  const managed = join(tmpdir(), 'sloppy-managed-identity');
  try {
    process.env.SLOPPY_IDENTITY_DIRECTORY = managed;
    assert.equal(
      new SloppyIdentity({ application: 'Example', pluginId: 'example' }).directory,
      managed,
    );
    assert.equal(
      new SloppyIdentity({
        application: 'Example',
        pluginId: 'example',
        directory: join(tmpdir(), 'explicit'),
      }).directory,
      join(tmpdir(), 'explicit'),
    );
  } finally {
    if (previous === undefined) delete process.env.SLOPPY_IDENTITY_DIRECTORY;
    else process.env.SLOPPY_IDENTITY_DIRECTORY = previous;
  }
});
