import test from 'node:test';
import assert from 'node:assert/strict';
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import childProcess, { spawnSync } from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { pathToFileURL } from 'node:url';
import {
  addRelease,
  removeRelease,
  validateCatalogue,
  writeCatalogue,
  readCatalogue,
  main,
} from '../catalogue.mjs';

import { validateIcon, setIcon } from '../plugin-setup.mjs';

function aliasedCLI(directory, script) {
  const packaged = join(directory, 'sdk-package');
  const alias = join(directory, 'sdk-alias');
  mkdirSync(packaged);
  for (const name of ['catalogue.mjs', 'plugin-setup.mjs'])
    copyFileSync(new URL(`../${name}`, import.meta.url), join(packaged, name));
  symlinkSync(packaged, alias, 'junction');
  return join(alias, script);
}

test('embedded icons are bounded PNGs, preserved by author tools and catalogues', () => {
  const png = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg==',
    'base64',
  );
  const uri = 'data:image/png;base64,' + png.toString('base64');
  validateIcon(uri);
  const directory = mkdtempSync(join(tmpdir(), 'sloppy-icon-'));
  try {
    writeFileSync(join(directory, 'plugin.json'), JSON.stringify(manifest()));
    writeFileSync(join(directory, 'icon.png'), png);
    setIcon(directory, join(directory, 'icon.png'));
    const source = JSON.parse(readFileSync(join(directory, 'plugin.json'), 'utf8'));
    assert.equal(source.plugin.icon, uri);
    const catalogue = addRelease(
      empty(),
      source,
      'https://example.com/plugins/example',
      'a'.repeat(40),
      '.',
      true,
    );
    assert.equal(catalogue.plugins[0].versions[0].manifest.plugin.icon, uri);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
  for (const invalid of [
    'https://example.com/icon.png',
    'data:image/svg+xml;base64,AAAA',
    'data:image/png;base64,AA==',
    uri + '!',
    'data:image/png;base64,' + 'A'.repeat(87400),
  ])
    assert.throws(() => validateIcon(invalid));
  const large = Buffer.from(png);
  large.writeUInt32BE(257, 16);
  assert.throws(() => validateIcon('data:image/png;base64,' + large.toString('base64')));
});

function empty() {
  return { catalogue_version: 1, type: 'sloppy.plugin-catalogue', name: 'Examples', plugins: [] };
}
function manifest(version = '1.0.0') {
  return {
    manifest_version: 1,
    protocol_version: 1,
    plugin: { id: 'example', name: 'Example', version },
    runtime: { kind: 'process', executables: { 'linux-x86_64': 'example.mjs' } },
  };
}
test('catalogue authoring retains manifests, pins releases and refuses implicit replacement', () => {
  const source = empty();
  const one = addRelease(
    source,
    manifest(),
    'https://example.com/plugins/example',
    'a'.repeat(40),
    '.',
    true,
  );
  const two = addRelease(
    one,
    manifest('2.0.0'),
    'https://example.com/plugins/example.git',
    'b'.repeat(40),
  );
  assert.deepEqual(source, empty());
  assert.equal(two.plugins[0].versions.length, 2);
  assert.throws(
    () => addRelease(two, manifest(), 'https://example.com/plugins/example', 'c'.repeat(40)),
    /immutable/,
  );
  assert.deepEqual(
    removeRelease(removeRelease(two, 'example', '1.0.0'), 'example', '2.0.0'),
    empty(),
  );
});
test('invalid catalogue edits preserve the previous file', () => {
  const directory = mkdtempSync(join(tmpdir(), 'sloppy-catalogue-'));
  try {
    const file = join(directory, 'catalogue.json');
    writeCatalogue(file, empty());
    const before = readFileSync(file);
    assert.throws(() => writeCatalogue(file, { ...empty(), catalogue_version: 2 }));
    assert.deepEqual(readFileSync(file), before);
    const valid = addRelease(
      empty(),
      manifest(),
      'https://example.com/plugins/example',
      'a'.repeat(40),
      '.',
      true,
    );
    for (const mutate of [
      (c) => {
        c.unexpected = true;
      },
      (c) => {
        c.plugins[0].repository = 'https://user:secret@example.com/repo';
      },
      (c) => {
        c.plugins[0].versions[0].commit = 'HEAD';
      },
      (c) => {
        c.plugins[0].versions[0].package_directory = '../outside';
      },
      (c) => {
        c.plugins[0].versions[0].manifest.plugin.version = '2.0.0';
      },
    ]) {
      const c = structuredClone(valid);
      mutate(c);
      assert.throws(() => validateCatalogue(c));
    }
    assert.deepEqual(readCatalogue(file), empty());
  } finally {
    rmSync(directory, { recursive: true });
  }
});
test('the shipped Node CLI authors catalogues without Python, SDK dependencies or Sloppy', () => {
  const directory = mkdtempSync(join(tmpdir(), 'sloppy-catalogue-cli-'));
  try {
    const cli = aliasedCLI(directory, 'catalogue.mjs');
    const file = join(directory, 'catalogue.json');
    const plugin = join(directory, 'plugin.json');
    writeFileSync(plugin, JSON.stringify(manifest()));
    for (const args of [
      ['init', file, '--name', 'Examples'],
      [
        'add-plugin',
        file,
        '--manifest',
        plugin,
        '--repository',
        'https://example.com/plugins/example',
        '--commit',
        'a'.repeat(40),
      ],
      ['validate', file],
      ['remove-version', file, 'example', '1.0.0'],
    ]) {
      const result = spawnSync(process.execPath, ['--preserve-symlinks-main', cli, ...args], {
        cwd: directory,
        encoding: 'utf8',
      });
      assert.equal(result.status, 0, result.stderr);
    }
    assert.deepEqual(readCatalogue(file).plugins, []);
  } finally {
    rmSync(directory, { recursive: true });
  }
});

test('Node setup installs a private empty dependency set and emits progress', () => {
  const directory = mkdtempSync(join(tmpdir(), 'sloppy-npm-setup-'));
  try {
    const cli = aliasedCLI(directory, 'plugin-setup.mjs');
    const value = manifest();
    value.dependencies = [{ runtime: 'node', minimum_version: '22' }];
    value.initialization = [{ kind: 'npm_install', directory: '.' }];
    writeFileSync(join(directory, 'plugin.json'), JSON.stringify(value));
    writeFileSync(
      join(directory, 'package.json'),
      JSON.stringify({ name: 'sloppy-setup-fixture', version: '1.0.0', private: true }),
    );
    const result = spawnSync(
      process.execPath,
      ['--preserve-symlinks-main', cli, 'prepare', directory, '--step', '0'],
      {
        cwd: new URL('../', import.meta.url),
        encoding: 'utf8',
        env: { ...process.env, npm_config_offline: 'true' },
      },
    );
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Downloading and installing Node.js dependencies/);
    assert.ok(readFileSync(join(directory, 'package-lock.json')).length);
  } finally {
    rmSync(directory, { recursive: true });
  }
});

test('Node setup uses the exact Git SDK commit instead of an unpublished registry package', () => {
  const directory = mkdtempSync(join(tmpdir(), 'sloppy-git-sdk-'));
  try {
    const sdk = join(directory, 'sdk');
    const plugin = join(directory, 'plugin');
    mkdirSync(sdk);
    mkdirSync(plugin);
    writeFileSync(
      join(sdk, 'package.json'),
      JSON.stringify({
        name: '@sloppy-plugins/sdk',
        version: '0.1.0',
        type: 'module',
        exports: './index.mjs',
      }),
    );
    writeFileSync(join(sdk, 'index.mjs'), 'export const ready = "pinned SDK";');
    const git = (...args) => {
      const r = spawnSync('git', ['-C', sdk, ...args], { encoding: 'utf8' });
      assert.equal(r.status, 0, r.stderr);
      return r.stdout.trim();
    };
    git('init');
    git('add', '.');
    git(
      '-c',
      'user.name=Fixture',
      '-c',
      'user.email=fixture@example.invalid',
      'commit',
      '-m',
      'Fixture',
    );
    const commit = git('rev-parse', 'HEAD');
    const value = manifest();
    value.dependencies = [
      { runtime: 'node', minimum_version: '22' },
      { runtime: 'git', minimum_version: '2.25' },
    ];
    value.initialization = [
      {
        kind: 'npm_install',
        sdk: { repository: 'https://fixture.invalid/sdk.git', commit, package_directory: '.' },
      },
    ];
    writeFileSync(join(plugin, 'plugin.json'), JSON.stringify(value));
    writeFileSync(
      join(plugin, 'package.json'),
      JSON.stringify({
        name: 'sloppy-pinned-sdk-fixture',
        version: '1.0.0',
        private: true,
        dependencies: { '@sloppy-plugins/sdk': '0.1.0' },
      }),
    );
    const url = pathToFileURL(sdk).href;
    const prepare = () =>
      spawnSync(process.execPath, ['plugin-setup.mjs', 'prepare', plugin], {
        cwd: new URL('../', import.meta.url),
        encoding: 'utf8',
        timeout: 45000,
        env: {
          ...process.env,
          npm_config_offline: 'true',
          GIT_CONFIG_COUNT: '1',
          GIT_CONFIG_KEY_0: `url.${url}.insteadOf`,
          GIT_CONFIG_VALUE_0: 'https://fixture.invalid/sdk.git',
          GIT_ALLOW_PROTOCOL: 'file',
        },
      });
    const result = prepare();
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /pinned Sloppy SDK/);
    const progress = result.stdout
      .split('\n')
      .filter((line) => line.startsWith('{'))
      .map((line) => JSON.parse(line));
    assert.ok(
      progress.some(
        (event) =>
          event.type === 'progress' &&
          event.message.includes(value.initialization[0].sdk.repository) &&
          event.message.includes(commit),
      ),
      'Download progress identifies the repository and pinned commit',
    );
    const imported = spawnSync(
      process.execPath,
      [
        '--input-type=module',
        '-e',
        'import {ready} from "@sloppy-plugins/sdk"; console.log(ready)',
      ],
      { cwd: plugin, encoding: 'utf8' },
    );
    assert.equal(imported.status, 0, imported.stderr);
    assert.match(imported.stdout, /pinned SDK/);
    const missingCommit = '0'.repeat(40);
    value.initialization[0].sdk.commit = missingCommit;
    writeFileSync(join(plugin, 'plugin.json'), JSON.stringify(value));
    rmSync(join(plugin, '.sloppy-sdk-source'), { recursive: true });
    const missing = prepare();
    assert.equal(missing.status, 1, missing.stdout);
    const failure = missing.stderr.split('\n').find((line) => line.startsWith('{'));
    const error = JSON.parse(failure);
    assert.equal(error.type, 'error');
    assert.ok(error.message.includes(value.initialization[0].sdk.repository));
    assert.ok(error.message.includes(missingCommit));
    assert.match(error.message, /commit has been published/);
  } finally {
    rmSync(directory, { recursive: true });
  }
});

test('Git metadata defaults infer nested packages and refuse uncommitted release manifests', () => {
  const directory = mkdtempSync(join(tmpdir(), 'sloppy-catalogue-git-'));
  try {
    const plugin = join(directory, 'packages', 'example');
    mkdirSync(plugin, { recursive: true });
    const descriptor = join(plugin, 'plugin.json');
    const file = join(directory, 'catalogue.json');
    writeFileSync(descriptor, JSON.stringify(manifest()));
    const git = (...args) => {
      const r = spawnSync('git', ['-C', directory, ...args], { encoding: 'utf8' });
      assert.equal(r.status, 0, r.stderr);
      return r.stdout.trim();
    };
    git('init');
    git('remote', 'add', 'origin', 'https://example.com/plugins/example.git');
    git('add', 'packages');
    git(
      '-c',
      'user.name=Fixture',
      '-c',
      'user.email=fixture@example.invalid',
      'commit',
      '-m',
      'Fixture',
    );
    writeCatalogue(file, empty());
    main(['add-plugin', file, '--manifest', descriptor]);
    const release = readCatalogue(file).plugins[0].versions[0];
    assert.equal(release.package_directory, 'packages/example');
    assert.equal(release.commit, git('rev-parse', 'HEAD'));
    const before = readFileSync(file);
    writeFileSync(descriptor, JSON.stringify(manifest('2.0.0')));
    assert.throws(
      () => main(['add-version', file, '--manifest', descriptor]),
      /Commit the plugin manifest first/,
    );
    assert.deepEqual(readFileSync(file), before);
    assert.throws(() => main(['validate', file, '--typo', 'x']), /Unknown option/);
  } finally {
    rmSync(directory, { recursive: true });
  }
});

test('Git metadata tolerates a repository root reported through another filesystem alias', (t) => {
  const temporary = mkdtempSync(join(tmpdir(), 'sloppy-catalogue-alias-'));
  const directory = join(temporary, 'repository');
  const alias = join(temporary, 'repository-alias');
  const plugin = join(directory, ' packages', 'a plugin café');
  mkdirSync(plugin, { recursive: true });
  symlinkSync(directory, alias, 'junction');
  const descriptor = join(plugin, 'plugin.json');
  const file = join(directory, 'catalogue.json');
  const git = (...args) => {
    const result = spawnSync('git', ['-C', directory, ...args], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  };
  let stub;
  try {
    writeFileSync(descriptor, JSON.stringify(manifest()));
    git('init');
    git('remote', 'add', 'origin', 'https://example.com/plugins/example.git');
    git('add', ' packages');
    git(
      '-c',
      'user.name=Fixture',
      '-c',
      'user.email=fixture@example.invalid',
      'commit',
      '-m',
      'Fixture',
    );
    writeCatalogue(file, empty());
    const original = childProcess.execFileSync;
    // Model Windows Git returning a long path while Node retains its 8.3 alias.
    stub = t.mock.method(childProcess, 'execFileSync', (program, args, options) =>
      program === 'git' && args.includes('--show-toplevel')
        ? alias
        : original(program, args, options),
    );
    syncBuiltinESMExports();
    main(['add-plugin', file, '--manifest', descriptor]);
    const release = readCatalogue(file).plugins[0].versions[0];
    assert.equal(release.package_directory, ' packages/a plugin café');
    assert.equal(release.commit, git('rev-parse', 'HEAD'));
    assert.deepEqual(release.manifest, manifest());
  } finally {
    stub?.mock.restore();
    syncBuiltinESMExports();
    rmSync(temporary, { recursive: true, force: true });
  }
});
