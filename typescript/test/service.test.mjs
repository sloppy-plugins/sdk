import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { request } from 'node:https';
import { X509Certificate } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { SloppyIdentity, SloppySession, certificateFingerprint } from '../dist/index.js';

test(
  'real service validates SDK operations and session isolation',
  { skip: !process.env.SLOPPY_SDK_SERVICE_BINARY, timeout: 10_000 },
  async (t) => {
    const directory = await mkdtemp(join(tmpdir(), 'sloppy-sdk-integration-'));
    const file = join(directory, 'connection.json');
    const ownerIdentity = new SloppyIdentity({
      application: 'sdk.integration',
      pluginId: 'copilot',
      directory: join(directory, 'owner'),
    });
    const otherIdentity = new SloppyIdentity({
      application: 'copilot',
      pluginId: 'copilot',
      directory: join(directory, 'other'),
    });
    const trusted = await Promise.all(
      [ownerIdentity, otherIdentity].map(async (identity) => ({
        fingerprint: await identity.fingerprint(),
        name: 'integration client',
        first_seen_at_ms: Date.now(),
        last_connected_at_ms: Date.now(),
        address: '127.0.0.1',
      })),
    );
    await writeFile(
      join(directory, 'client-trust.json'),
      JSON.stringify({
        version: 1,
        allow_remote_clients: false,
        ignore_unknown_clients: false,
        trusted_clients: Object.fromEntries(trusted.map((entry) => [entry.fingerprint, entry])),
        rejected_clients: {},
      }),
    );
    const server = spawn(
      process.env.SLOPPY_SDK_SERVICE_BINARY,
      ['--port', '0', '--connection-file', file],
      { stdio: ['ignore', 'ignore', 'pipe'] },
    );
    let stderr = '';
    server.stderr.on('data', (data) => {
      stderr += data;
    });
    t.after(async () => {
      if (server.exitCode === null) {
        const ended = once(server, 'exit');
        server.kill();
        await ended;
      }
      await rm(directory, { recursive: true });
    });
    let connection;
    const deadline = Date.now() + 3_000;
    while (!connection) {
      assert.equal(server.exitCode, null, stderr);
      assert.ok(Date.now() < deadline, 'Service startup timed out');
      try {
        connection = JSON.parse(await readFile(file, 'utf8'));
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
      if (!connection) await new Promise((resolve) => setTimeout(resolve, 10));
    }
    const serviceIdentity = JSON.parse(
      await readFile(join(directory, 'service-identity', 'identity.json'), 'utf8'),
    );
    const serverFingerprint = certificateFingerprint(
      new X509Certificate(serviceIdentity.certificate_pem).raw,
    );
    const owner = new SloppySession({
      pluginId: 'copilot',
      application: 'sdk.integration',
      connectionFile: file,
      identityDirectory: ownerIdentity.directory,
      confirmServer: (service) => service.fingerprint === serverFingerprint,
    });
    const other = new SloppySession({
      pluginId: 'copilot',
      connectionFile: file,
      identityDirectory: otherIdentity.directory,
      confirmServer: (service) => service.fingerprint === serverFingerprint,
    });
    t.after(() => {
      owner.close();
      other.close();
    });
    await Promise.all([owner.ready, other.ready]);
    const bubble = owner.send({
      kind: 'question',
      title: 'Owned',
      message: 'Only this session can answer',
      requires_response: true,
      response: { value_type: 'text', allow_custom: true, choices: [] },
    });
    const created = await bubble.created;
    assert.equal(created.session_id, owner.sessionId);
    await assert.rejects(other.get(bubble.id), { status: 404 });
    for (const [method, path] of [
      ['GET', '/v1/notifications'],
      ['POST', '/v1/notifications'],
      ['GET', `/v1/notifications/${bubble.id}`],
      ...['answers', 'resolutions', 'acknowledgements', 'cancel'].map((operation) => [
        'POST',
        `/v1/notifications/${bubble.id}/${operation}`,
      ]),
    ]) {
      const identity = await ownerIdentity.load();
      const status = await new Promise((resolve, reject) => {
        const call = request(
          connection.base_url + path,
          {
            method,
            headers: { Authorization: `Bearer ${connection.token}` },
            cert: identity.certificate_pem,
            key: identity.private_key_pem,
            ca: serviceIdentity.certificate_pem,
            minVersion: 'TLSv1.3',
            checkServerIdentity: (_host, certificate) =>
              certificateFingerprint(certificate.raw) === serverFingerprint
                ? undefined
                : new Error('Service certificate changed'),
          },
          (response) => {
            response.resume();
            resolve(response.statusCode);
          },
        );
        call.on('error', reject);
        call.end();
      });
      assert.equal(status, 404);
    }
    assert.deepEqual(await other.list(), []);
    const submitted = await owner.answer(bubble.id, { value: 'Proposal' });
    assert.equal(submitted.state, 'submitted');
    assert.equal((await bubble).value, 'Proposal');
    assert.equal((await owner.acknowledge(bubble.id, 'accepted')).state, 'submitted');
    assert.equal((await owner.resolve(bubble.id, { value: 'Completed' })).state, 'resolved');
    assert.equal((await owner.get(bubble.id)).resolution.answer.source, 'sdk.integration');
    const serviceLog = join(directory, 'logs', 'sessions', `${owner.sessionId}.log`);
    const contents = await readFile(serviceLog, 'utf8');
    const records = contents.trim().split('\n').map(JSON.parse);
    assert.ok(records.some((line) => line.event === 'connected'));
    assert.ok(records.some((line) => line.event === 'ready' && line.level === 'info'));
    assert.ok(records.every((line) => line.level !== 'debug'));
    for (const secret of [
      connection.token,
      owner.resumeCredentials.resume_token,
      'resume_token',
      'Only this session can answer',
      'Proposal',
      'Completed',
    ])
      assert.ok(!contents.includes(secret), secret);
    if (process.platform !== 'win32') assert.equal((await stat(serviceLog)).mode & 0o777, 0o600);
    const aggregate = await readFile(join(directory, 'logs', 'server.log'), 'utf8');
    assert.ok(aggregate.includes('"service_started"'));
    assert.ok(aggregate.includes(owner.sessionId));
    assert.ok(aggregate.includes(other.sessionId));
    assert.ok(!aggregate.includes(connection.token));
    assert.ok(!aggregate.includes('Only this session can answer'));
  },
);
