#!/usr/bin/env node
// Catalogue authoring stays independent of the Sloppy application and services.
import {
  readFileSync,
  writeFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  renameSync,
  rmSync,
  realpathSync,
} from 'node:fs';
import { basename, dirname, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { validateManifest } from './plugin-setup.mjs';

const semver =
  /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/;
export function repositoryURL(value) {
  const url = new URL(value);
  if (
    url.protocol !== 'https:' ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !url.pathname.replaceAll('/', '') ||
    /\s/.test(value)
  )
    throw new Error(
      'Repositories require HTTPS URLs without embedded credentials, queries or fragments',
    );
  return value.replace(/\/$/, '').replace(/\.git$/, '') + '.git';
}
export function validateCatalogue(catalogue) {
  function fields(value, allowed) {
    if (
      !value ||
      typeof value !== 'object' ||
      Array.isArray(value) ||
      Object.keys(value).some((key) => !allowed.includes(key))
    )
      throw new Error('Unexpected catalogue fields or object type');
  }
  fields(catalogue, ['catalogue_version', 'type', 'name', 'description', 'plugins']);
  if (
    catalogue.catalogue_version !== 1 ||
    catalogue.type !== 'sloppy.plugin-catalogue' ||
    typeof catalogue.name !== 'string' ||
    !catalogue.name.trim()
  )
    throw new Error('Expected a named version-1 Sloppy plugin catalogue');
  if (!Array.isArray(catalogue.plugins) || catalogue.plugins.length > 512)
    throw new Error('Invalid plugin list');
  const ids = new Set();
  for (const plugin of catalogue.plugins) {
    fields(plugin, ['id', 'name', 'description', 'repository', 'versions']);
    if (
      !/^[a-z0-9-]{1,64}$/.test(plugin.id ?? '') ||
      ids.has(plugin.id) ||
      typeof plugin.name !== 'string' ||
      !plugin.name.trim()
    )
      throw new Error('Plugin IDs must be valid/unique and names must be nonempty');
    ids.add(plugin.id);
    repositoryURL(plugin.repository);
    if (
      !Array.isArray(plugin.versions) ||
      plugin.versions.length < 1 ||
      plugin.versions.length > 256
    )
      throw new Error('Each plugin needs 1–256 releases');
    const versions = new Set();
    for (const release of plugin.versions) {
      fields(release, ['version', 'commit', 'package_directory', 'manifest']);
      if (
        !semver.test(release.version ?? '') ||
        versions.has(release.version) ||
        !/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(release.commit ?? '')
      )
        throw new Error('Releases need unique semantic versions and full Git commit hashes');
      versions.add(release.version);
      const path = release.package_directory ?? '.';
      if (
        typeof path !== 'string' ||
        !path ||
        path.startsWith('/') ||
        path.includes('\\') ||
        path.split('/').includes('..')
      )
        throw new Error('Package paths must be relative without traversal');
      const manifest = validateManifest(release.manifest);
      if (
        manifest?.manifest_version !== 1 ||
        !Number.isInteger(manifest.protocol_version) ||
        manifest.protocol_version < 1 ||
        manifest.plugin?.id !== plugin.id ||
        manifest.plugin?.version !== release.version
      )
        throw new Error('Catalogue identity/version disagrees with its version-1 manifest');
    }
  }
  return catalogue;
}
export function readCatalogue(file) {
  const data = readFileSync(file);
  if (data.length > 8 * 1024 * 1024) throw new Error('Catalogue exceeds the 8 MB limit');
  return validateCatalogue(JSON.parse(data));
}
export function writeCatalogue(file, catalogue) {
  validateCatalogue(catalogue);
  if (existsSync(file) && lstatSync(file).isSymbolicLink())
    throw new Error('Refusing to replace a symbolic-link catalogue');
  mkdirSync(dirname(resolve(file)), { recursive: true });
  const data = JSON.stringify(catalogue, null, 2) + '\n';
  if (Buffer.byteLength(data) > 8 * 1024 * 1024)
    throw new Error('Catalogue exceeds the 8 MB limit');
  const temporary = resolve(dirname(file), `.catalogue-${randomUUID()}.tmp`);
  try {
    writeFileSync(temporary, data, { flag: 'wx', mode: 0o600 });
    renameSync(temporary, file);
  } finally {
    rmSync(temporary, { force: true });
  }
}
export function addRelease(
  catalogue,
  manifest,
  repository,
  commit,
  packageDirectory = '.',
  newPlugin = false,
) {
  const next = structuredClone(catalogue);
  const descriptor = manifest.plugin;
  let plugin = next.plugins.find((p) => p.id === descriptor.id);
  if (newPlugin && plugin) throw new Error('Plugin already exists; use add-version');
  if (!newPlugin && !plugin) throw new Error('Plugin is missing; use add-plugin');
  const url = repositoryURL(repository);
  if (!plugin) {
    plugin = {
      id: descriptor.id,
      name: descriptor.name,
      description: descriptor.description ?? '',
      repository: url,
      versions: [],
    };
    next.plugins.push(plugin);
  } else if (repositoryURL(plugin.repository) !== url)
    throw new Error('Plugin repository differs from the existing entry');
  if (plugin.versions.some((v) => v.version === descriptor.version))
    throw new Error('This version already exists; published versions are immutable');
  plugin.versions.push({
    version: descriptor.version,
    commit: commit.toLowerCase(),
    package_directory: packageDirectory,
    manifest: structuredClone(manifest),
  });
  return validateCatalogue(next);
}
export function removeRelease(catalogue, id, version) {
  const next = structuredClone(catalogue);
  const plugin = next.plugins.find((p) => p.id === id);
  if (!plugin?.versions.some((v) => v.version === version))
    throw new Error('That plugin/version is not in the catalogue');
  plugin.versions = plugin.versions.filter((v) => v.version !== version);
  if (!plugin.versions.length) next.plugins = next.plugins.filter((p) => p.id !== id);
  return validateCatalogue(next);
}
function git(directory, ...args) {
  return execFileSync('git', ['-C', directory, ...args], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  }).replace(/\r?\n$/, '');
}
export function main(args) {
  const [operation, file, ...rest] = args;
  if (
    !file ||
    !['init', 'validate', 'add-plugin', 'add-version', 'remove-version'].includes(operation)
  )
    throw new Error(
      'Usage: sloppy-catalogue init|validate|add-plugin|add-version|remove-version FILE [options]',
    );
  const options = {};
  if (operation !== 'remove-version')
    for (let i = 0; i < rest.length; i += 2) {
      if (!rest[i].startsWith('--') || rest[i + 1] === undefined)
        throw new Error('Expected --option VALUE');
      options[rest[i].slice(2)] = rest[i + 1];
    }
  const allowed =
    operation === 'init'
      ? ['name', 'description']
      : ['add-plugin', 'add-version'].includes(operation)
        ? ['manifest', 'repository', 'commit', 'package-directory']
        : [];
  for (const option of Object.keys(options))
    if (!allowed.includes(option)) throw new Error(`Unknown option --${option}`);
  if (operation === 'remove-version' && rest.length !== 2)
    throw new Error('Expected plugin ID and version');
  if (operation === 'init') {
    if (existsSync(file)) throw new Error('Catalogue already exists; it was not overwritten');
    writeCatalogue(file, {
      catalogue_version: 1,
      type: 'sloppy.plugin-catalogue',
      name: options.name ?? '',
      description: options.description ?? '',
      plugins: [],
    });
  } else if (operation === 'validate') {
    const catalogue = readCatalogue(file);
    console.log(`Valid Sloppy catalogue: ${catalogue.name} (${catalogue.plugins.length} plugins)`);
    return;
  } else if (operation === 'remove-version')
    writeCatalogue(file, removeRelease(readCatalogue(file), rest[0], rest[1]));
  else {
    if (!options.manifest) throw new Error('--manifest PATH is required');
    const manifest = JSON.parse(readFileSync(options.manifest, 'utf8'));
    const manifestPath = realpathSync.native(options.manifest);
    const directory = dirname(manifestPath);
    const repository = options.repository ?? git(directory, 'remote', 'get-url', 'origin');
    const commit = options.commit ?? git(directory, 'rev-parse', 'HEAD');
    let packageDirectory = options['package-directory'];
    if (!options.commit) {
      // Git resolves Windows short names and working-tree aliases itself.
      const prefix = git(directory, 'rev-parse', '--show-prefix');
      packageDirectory ??= prefix.replace(/\/$/, '') || '.';
      const path = prefix + basename(manifestPath);
      const committed = JSON.parse(git(directory, 'show', `${commit}:${path}`));
      if (!isDeepStrictEqual(committed, manifest))
        throw new Error('Commit the plugin manifest first, or explicitly provide --commit');
    }
    writeCatalogue(
      file,
      addRelease(
        readCatalogue(file),
        manifest,
        repository,
        commit,
        packageDirectory ?? '.',
        operation === 'add-plugin',
      ),
    );
  }
  console.log(`Updated ${file}; no Git commit or push was performed.`);
}
if (
  process.argv[1] &&
  realpathSync.native(process.argv[1]) === realpathSync.native(fileURLToPath(import.meta.url))
) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    console.error(`Catalogue update failed: ${error.message}`);
    process.exitCode = 1;
  }
}
