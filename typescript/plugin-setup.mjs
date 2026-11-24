#!/usr/bin/env node
// Standalone bootstrap: it runs before the plugin's npm dependencies exist.
import { readFileSync, writeFileSync, renameSync, rmSync, existsSync, realpathSync } from 'node:fs';
import { resolve, relative, isAbsolute, join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export function validateIcon(value) {
  const message = 'Plugin icon must be a base64 PNG data URI, at most 64 KiB and 256×256 pixels';
  if (
    typeof value !== 'string' ||
    value.length > 87406 ||
    !value.startsWith('data:image/png;base64,')
  )
    throw new Error(message);
  const encoded = value.slice(22),
    data = Buffer.from(encoded, 'base64');
  if (
    data.toString('base64') !== encoded ||
    data.length < 33 ||
    data.length > 65536 ||
    !data.subarray(0, 16).equals(Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex')) ||
    data.readUInt32BE(16) < 1 ||
    data.readUInt32BE(16) > 256 ||
    data.readUInt32BE(20) < 1 ||
    data.readUInt32BE(20) > 256
  )
    throw new Error(message);
}
export function setIcon(directory, image) {
  const manifest = inspectManifest(directory);
  manifest.plugin.icon = 'data:image/png;base64,' + readFileSync(image).toString('base64');
  validateManifest(manifest);
  const target = join(resolve(directory), 'plugin.json');
  const temporary = target + '.' + process.pid + '.tmp';
  try {
    writeFileSync(temporary, JSON.stringify(manifest, null, 2) + '\n', { flag: 'wx' });
    renameSync(temporary, target);
  } finally {
    rmSync(temporary, { force: true });
  }
}

export function gitDependency(value) {
  if (
    !value ||
    typeof value !== 'object' ||
    Object.keys(value).some((key) => !['repository', 'commit', 'package_directory'].includes(key))
  )
    throw new Error('SDK sources need repository, commit and package_directory');
  const url = new URL(value.repository);
  if (
    url.protocol !== 'https:' ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !url.pathname.replaceAll('/', '')
  )
    throw new Error('SDK repositories require HTTPS without embedded credentials');
  if (!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(value.commit ?? ''))
    throw new Error('SDK sources must be pinned to a full Git commit hash');
  relativePath(value.package_directory);
  return value;
}
function relativePath(value) {
  if (
    typeof value !== 'string' ||
    !value ||
    value.startsWith('/') ||
    value.includes('\\') ||
    value.split('/').includes('..') ||
    [...value].some((character) => character.codePointAt(0) < 32)
  )
    throw new Error('Setup paths must be package-relative without traversal');
}
export function validateManifest(manifest) {
  if (
    !manifest ||
    typeof manifest !== 'object' ||
    Object.keys(manifest).some(
      (key) =>
        ![
          'manifest_version',
          'protocol_version',
          'plugin',
          'runtime',
          'actions',
          'dependencies',
          'initialization',
          'background',
        ].includes(key),
    )
  )
    throw new Error('Unknown plugin manifest fields');
  if (
    manifest.manifest_version !== 1 ||
    !Number.isInteger(manifest.protocol_version) ||
    manifest.protocol_version < 1
  )
    throw new Error('Expected a version-1 plugin manifest with protocol_version');
  for (const field of ['id', 'name', 'version'])
    if (typeof manifest.plugin?.[field] !== 'string' || !manifest.plugin[field].trim())
      throw new Error(`Plugin ${field} is required`);
  if (
    !/^[a-z0-9-]{1,64}$/.test(manifest.plugin.id) ||
    ['plugins', 'trust'].includes(manifest.plugin.id)
  )
    throw new Error('Invalid or reserved plugin ID');
  if ('icon' in manifest.plugin) validateIcon(manifest.plugin.icon);
  const runtime = manifest.runtime;
  if (
    runtime?.kind !== 'process' ||
    !runtime.executables ||
    typeof runtime.executables !== 'object' ||
    !Object.keys(runtime.executables).length
  )
    throw new Error('A process runtime with platform executables is required');
  for (const path of Object.values(runtime.executables)) relativePath(path);
  const dependencies = manifest.dependencies ?? [];
  if (!Array.isArray(dependencies) || dependencies.length > 8)
    throw new Error('Invalid dependency list');
  const seen = new Set();
  for (const dependency of dependencies) {
    if (
      !dependency ||
      Object.keys(dependency).some((key) => !['runtime', 'minimum_version'].includes(key)) ||
      !['python', 'node', 'git'].includes(dependency.runtime) ||
      seen.has(dependency.runtime) ||
      !/^\d+(?:\.\d+){0,2}$/.test(dependency.minimum_version ?? '')
    )
      throw new Error('Dependencies need unique runtimes and numeric minimum versions');
    seen.add(dependency.runtime);
  }
  const steps = manifest.initialization ?? [];
  if (!Array.isArray(steps) || steps.length > 32) throw new Error('Invalid initialization steps');
  for (const step of steps) {
    const allowed = {
      python_venv: ['kind', 'directory', 'requirements', 'sdk'],
      npm_install: ['kind', 'directory', 'sdk'],
      command: ['kind', 'program', 'arguments', 'directory', 'if_missing'],
    };
    if (
      !step ||
      !allowed[step.kind] ||
      Object.keys(step).some((key) => !allowed[step.kind].includes(key))
    )
      throw new Error('Unknown initialization step or fields');
    relativePath(step.directory ?? (step.kind === 'python_venv' ? '.venv' : '.'));
    if (step.kind === 'python_venv') {
      relativePath(step.requirements);
      if (!seen.has('python') || step.directory === '.')
        throw new Error(
          'Python setup needs a declared Python runtime and its own environment directory',
        );
    }
    if (step.kind === 'npm_install' && !seen.has('node'))
      throw new Error('npm setup needs a declared Node runtime');
    if (step.sdk) {
      gitDependency(step.sdk);
      if (!seen.has('git')) throw new Error('Git SDK sources need a declared Git dependency');
    }
    if (step.kind === 'command') {
      if (
        typeof step.program !== 'string' ||
        !step.program ||
        /[/\\]/.test(step.program) ||
        !Array.isArray(step.arguments) ||
        step.arguments.some((arg) => typeof arg !== 'string' || arg.includes('\0'))
      )
        throw new Error('Commands need an executable name and an argument array');
      if (step.if_missing) relativePath(step.if_missing);
    }
  }
  if (manifest.actions) {
    const packaged = manifest.actions;
    const catalog = packaged.catalog;
    if (
      typeof packaged.provider_key !== 'string' ||
      !packaged.provider_key ||
      catalog?.actions_version !== 1 ||
      !Array.isArray(catalog.actions)
    )
      throw new Error('Invalid action catalogue declaration');
    const keys = new Set();
    for (const action of catalog.actions) {
      if (
        typeof action.key !== 'string' ||
        !action.key ||
        keys.has(action.key) ||
        typeof action.title !== 'string' ||
        !action.title.trim()
      )
        throw new Error('Actions need unique keys and titles');
      keys.add(action.key);
      const schema = action.input_schema;
      if (
        schema?.type !== 'object' ||
        schema.additionalProperties !== false ||
        !schema.properties ||
        Object.keys(schema.properties).length ||
        Object.keys(schema).length !== 3
      )
        throw new Error('This Actions version supports empty-object invocation inputs');
      if (action.interaction === 'input_bubble' && action.prompt?.type !== 'questionnaire')
        throw new Error('Input actions must declare their questionnaire prompt');
    }
  }
  return manifest;
}
export function inspectManifest(directory) {
  const root = realpathSync(directory);
  const manifest = validateManifest(JSON.parse(readFileSync(join(root, 'plugin.json'), 'utf8')));
  for (const step of manifest.initialization ?? []) packagePath(root, step.directory ?? '.');
  return manifest;
}
function packagePath(root, value) {
  if (typeof value !== 'string' || value.includes('\\'))
    throw new Error('Setup paths must be package-relative');
  const target = resolve(root, value);
  const path = relative(root, target);
  if (path.startsWith('..') || isAbsolute(path))
    throw new Error('Setup path escapes the copied plugin');
  let ancestor = target;
  while (!existsSync(ancestor)) ancestor = resolve(ancestor, '..');
  const actual = realpathSync(ancestor);
  const actualPath = relative(root, actual);
  if (actualPath.startsWith('..') || isAbsolute(actualPath))
    throw new Error('Setup path follows a link outside the plugin');
  return target;
}
export async function prepareStep(directory, index = 0) {
  const root = realpathSync(directory);
  const manifest = inspectManifest(root);
  const step = (manifest.initialization ?? [])[index];
  if (step?.kind !== 'npm_install') throw new Error('This helper prepares npm_install steps');
  const cwd = packagePath(root, step.directory ?? '.');
  if (!existsSync(join(cwd, 'package.json')))
    throw new Error('The declared package.json is missing');
  const run = (program, args, cwd, capture = false) =>
    new Promise((accept, reject) => {
      let output = '';
      const child = spawn(program, args, {
        cwd,
        stdio: capture ? ['ignore', 'pipe', 'inherit'] : 'inherit',
        shell: process.platform === 'win32' && program.endsWith('.cmd'),
        env: {
          ...process.env,
          npm_config_global: 'false',
          ...(program === npm ? { npm_config_prefix: cwd } : {}),
        },
      });
      if (capture)
        child.stdout.on('data', (data) => {
          output += data;
        });
      child.on('error', reject);
      child.on('exit', (code) =>
        code === 0 ? accept(output) : reject(new Error(`${program} failed (${code})`)),
      );
    });
  const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  let sdkDirectory;
  if (step.sdk) {
    const sdk = gitDependency(step.sdk);
    console.log(
      JSON.stringify({
        type: 'progress',
        message: `Downloading the pinned Sloppy SDK from ${sdk.repository} (commit ${sdk.commit})…`,
      }),
    );
    const source = join(root, '.sloppy-sdk-source');
    if (existsSync(source))
      throw new Error('SDK source directory already exists; prepare a clean plugin copy');
    await run('git', ['init', source], root);
    await run('git', ['remote', 'add', 'origin', sdk.repository], source);
    try {
      await run('git', ['fetch', '--depth=1', 'origin', sdk.commit], source);
    } catch (error) {
      throw new Error(
        `Could not download the Sloppy SDK from ${sdk.repository} at commit ${sdk.commit}: ${error.message}. Check repository access and that this commit has been published.`,
        { cause: error },
      );
    }
    await run('git', ['checkout', '--detach', 'FETCH_HEAD'], source);
    if (
      (await run('git', ['rev-parse', 'HEAD'], source, true)).trim().toLowerCase() !==
      sdk.commit.toLowerCase()
    )
      throw new Error('SDK Git commit mismatch');
    sdkDirectory = packagePath(source, sdk.package_directory);
    const sdkPackage = JSON.parse(readFileSync(join(source, 'package.json'), 'utf8'));
    console.log(
      JSON.stringify({
        type: 'progress',
        message: 'Preparing the Sloppy SDK and its dependencies…',
      }),
    );
    await run(
      npm,
      [existsSync(join(source, 'package-lock.json')) ? 'ci' : 'install', '--no-audit', '--no-fund'],
      source,
    );
    if (sdkPackage.scripts?.build) await run(npm, ['run', 'build'], source);
  }
  console.log(
    JSON.stringify({
      type: 'progress',
      message: 'Downloading and installing Node.js dependencies…',
    }),
  );
  const args = sdkDirectory
    ? ['install', '--no-save', '--package-lock=false', '--workspaces=false', sdkDirectory]
    : [existsSync(join(cwd, 'package-lock.json')) ? 'ci' : 'install'];
  await run(npm, [...args, '--no-audit', '--no-fund'], cwd);
  console.log(JSON.stringify({ type: 'progress', message: 'Node.js dependencies are ready.' }));
}
if (
  process.argv[1] &&
  realpathSync.native(process.argv[1]) === realpathSync.native(fileURLToPath(import.meta.url))
) {
  try {
    const [operation, directory, option, index] = process.argv.slice(2);
    if (!directory || !['validate', 'prepare', 'set-icon'].includes(operation))
      throw new Error(
        'Usage: sloppy-plugin-setup validate|prepare|set-icon DIRECTORY [--step N | --icon PATH.png]',
      );
    if (operation === 'validate') console.log(JSON.stringify(inspectManifest(directory), null, 2));
    else if (operation === 'set-icon') {
      if (option !== '--icon' || !index) throw new Error('set-icon requires --icon PATH.png');
      setIcon(directory, index);
    } else {
      if (option && option !== '--step') throw new Error('Expected --step N');
      await prepareStep(directory, Number(index ?? 0));
    }
  } catch (error) {
    console.error(JSON.stringify({ type: 'error', message: error.message }));
    process.exitCode = 1;
  }
}
