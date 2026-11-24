import assert from 'node:assert/strict';
import { chmod, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { SloppySession } from '../dist/index.js';

async function offline(t, contents, mode = 0o600) {
  const directory = await mkdtemp(join(tmpdir(), 'sloppy-errors-'));
  const file = join(directory, 'connection.json');
  if (contents !== undefined) await writeFile(file, contents, { mode });
  const session = new SloppySession({
    pluginId: 'testing',
    endpoint: '',
    connectionFile: file,
    identityDirectory: join(directory, 'identity'),
    retryDelayMs: 10,
    maxRetryDelayMs: 30,
  });
  const statuses = [];
  session.on('status', (status) => statuses.push(status));
  t.after(async () => {
    session.close();
    await chmod(file, 0o600).catch(() => {});
    await rm(directory, { recursive: true });
  });
  async function status() {
    const deadline = Date.now() + 3000;
    while (!statuses.some((item) => item.error)) {
      assert.ok(Date.now() < deadline, 'SDK did not report the connection problem');
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    return statuses.find((item) => item.error);
  }
  return { file, status };
}

test('missing discovery explains disabled service and custom paths without claiming the app is closed', async (t) => {
  const f = await offline(t);
  const status = await f.status();
  assert.equal(status.code, 'service_unavailable');
  assert.match(status.error, /Enable Sloppy service server/);
  assert.match(status.error, /custom connection file/);
  assert.ok(!status.error.includes('ENOENT'));
  assert.ok(!status.error.includes('Sloppy is not running'));
});

test('malformed discovery is actionable and never echoes file credentials', async (t) => {
  const f = await offline(t, '{"token":"DO_NOT_ECHO_PRIVATE_TOKEN", broken}');
  const status = await f.status();
  assert.equal(status.code, 'discovery_error');
  assert.match(status.error, /not valid service discovery JSON/);
  assert.match(status.error, /Restart Sloppy/);
  assert.ok(!status.error.includes('DO_NOT_ECHO_PRIVATE_TOKEN'));
});

test(
  'unreadable discovery is a permission failure, not a disabled-service diagnosis',
  {
    skip: process.platform === 'win32' || process.getuid?.() === 0,
  },
  async (t) => {
    const f = await offline(t, '{}', 0);
    const status = await f.status();
    assert.equal(status.code, 'discovery_error');
    assert.match(status.error, /permission denied/);
    assert.match(status.error, /permissions/);
    assert.ok(!status.error.includes('service server'));
  },
);
