import 'reflect-metadata';
import {
  createHash,
  createPrivateKey,
  randomBytes,
  randomUUID,
  X509Certificate,
} from 'node:crypto';
import { lstat, mkdir, open, readFile, rename, rmdir, unlink } from 'node:fs/promises';
import { homedir } from 'node:os';
import { isAbsolute, join } from 'node:path';
import { connect, type TLSSocket } from 'node:tls';
import { setTimeout as sleep } from 'node:timers/promises';
import {
  BasicConstraintsExtension,
  cryptoProvider,
  ExtendedKeyUsageExtension,
  KeyUsageFlags,
  KeyUsagesExtension,
  Name,
  X509CertificateGenerator,
} from '@peculiar/x509';
import { SloppyError } from './errors.js';

cryptoProvider.set(globalThis.crypto);

interface IdentityData {
  version: number;
  name: string;
  certificate_pem: string;
  private_key_pem: string;
}
export interface PresentedService {
  endpoint: string;
  fingerprint: string;
  certificate_pem: string;
  name: string;
}
export interface KnownService extends PresentedService {
  created_at_ms: number;
  last_connected_at_ms: number | null;
}
interface KnownServices {
  version: number;
  services: Record<string, KnownService>;
}
export interface IdentityOptions {
  directory?: string;
  profile?: string;
  application: string;
  pluginId: string;
}

export class SloppyTrustRequiredError extends SloppyError {
  constructor(
    public readonly presented: PresentedService,
    public readonly knownFingerprint?: string,
  ) {
    super(
      knownFingerprint ? 'server_identity_changed' : 'server_trust_required',
      knownFingerprint
        ? `Sloppy certificate changed at ${presented.endpoint}: expected ${knownFingerprint}, received ${presented.fingerprint}. Compare the new fingerprint in Sloppy Settings before explicitly replacing the saved certificate.`
        : `Confirm Sloppy certificate at ${presented.endpoint}: ${presented.fingerprint}. Compare this fingerprint in Sloppy Settings, then explicitly approve this service.`,
    );
    this.name = 'SloppyTrustRequiredError';
  }
}

export function certificateFingerprint(der: Uint8Array): string {
  return 'SHA256:' + createHash('sha256').update(der).digest('base64').replace(/=+$/, '');
}

export function canonicalEndpoint(endpoint: string): string {
  let url: URL;
  try {
    url = new URL(endpoint);
  } catch {
    throw new SloppyError('invalid_input', 'Invalid Sloppy service endpoint');
  }
  if (
    url.protocol !== 'https:' ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== '/' ||
    url.port === '0' ||
    Array.from(endpoint).some((char) => char.charCodeAt(0) <= 32 || char.charCodeAt(0) === 127)
  ) {
    throw new SloppyError(
      'invalid_input',
      'Service endpoint must be an HTTPS authority without credentials or a path',
    );
  }
  return url.origin;
}

export function identityDirectory(options: IdentityOptions): string {
  const directory = options.directory ?? process.env.SLOPPY_IDENTITY_DIRECTORY;
  if (directory !== undefined) {
    if (!isAbsolute(directory))
      throw new SloppyError('invalid_input', 'Identity directory must be absolute');
    return directory;
  }
  const profile =
    options.profile ??
    process.env.SLOPPY_IDENTITY_PROFILE ??
    createHash('sha256')
      .update(options.application)
      .update('\0')
      .update(options.pluginId)
      .digest('hex');
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(profile))
    throw new SloppyError('invalid_input', 'Invalid identity profile');
  const root =
    process.platform === 'darwin'
      ? join(homedir(), 'Library/Application Support/Sloppy')
      : process.platform === 'win32'
        ? process.env.APPDATA
          ? join(process.env.APPDATA, 'Sloppy')
          : undefined
        : join(process.env.XDG_DATA_HOME ?? join(homedir(), '.local/share'), 'sloppy');
  if (!root) throw new SloppyError('identity_error', 'APPDATA is not set');
  return join(root, 'identities', profile);
}

async function locked<T>(directory: string, action: () => Promise<T>): Promise<T> {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const lock = join(directory, '.security-lock');
  const deadline = Date.now() + 5_000;
  for (;;) {
    try {
      await mkdir(lock, { mode: 0o700 });
      break;
    } catch (error) {
      if (!(error instanceof Error) || !('code' in error) || error.code !== 'EEXIST') throw error;
      if (Date.now() >= deadline)
        throw new SloppyError(
          'identity_error',
          `Security storage is locked: ${lock}; if its owner exited, remove this empty lock directory`,
        );
      await sleep(25);
    }
  }
  try {
    return await action();
  } finally {
    await rmdir(lock);
  }
}

async function readOptional(file: string): Promise<unknown | undefined> {
  try {
    return JSON.parse(await readFile(file, 'utf8')) as unknown;
  } catch (error) {
    if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return undefined;
    throw new SloppyError(
      'identity_error',
      `Cannot read security storage: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

async function privateJson(directory: string, file: string, value: unknown): Promise<void> {
  const destination = join(directory, file);
  try {
    if ((await lstat(destination)).isSymbolicLink())
      throw new SloppyError('identity_error', 'Refusing to replace a symlink');
  } catch (error) {
    if (!(error instanceof Error) || !('code' in error) || error.code !== 'ENOENT') throw error;
  }
  const temporary = join(directory, `.sloppy-${randomUUID()}.tmp`);
  const handle = await open(temporary, 'wx', 0o600);
  try {
    await handle.writeFile(JSON.stringify(value, null, 2));
    await handle.sync();
  } catch (error) {
    await handle.close();
    await unlink(temporary);
    throw error;
  }
  await handle.close();
  try {
    await rename(temporary, destination);
  } catch (error) {
    await unlink(temporary);
    throw error;
  }
}

function identityData(value: unknown): IdentityData {
  if (!value || typeof value !== 'object')
    throw new SloppyError('identity_error', 'Invalid identity file');
  const data = value as Partial<IdentityData>;
  if (
    data.version !== 1 ||
    typeof data.name !== 'string' ||
    typeof data.certificate_pem !== 'string' ||
    typeof data.private_key_pem !== 'string'
  )
    throw new SloppyError('identity_error', 'Invalid identity file');
  try {
    const certificate = new X509Certificate(data.certificate_pem);
    if (!certificate.checkPrivateKey(createPrivateKey(data.private_key_pem)))
      throw new Error('Identity certificate and key do not match');
  } catch (error) {
    throw new SloppyError(
      'identity_error',
      `Invalid identity key pair: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  return data as IdentityData;
}

async function generate(name: string): Promise<IdentityData> {
  if (
    !name.trim() ||
    Buffer.byteLength(name) > 64 ||
    Array.from(name).some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
  )
    throw new SloppyError('invalid_input', 'Invalid certificate name');
  const algorithm = { name: 'ECDSA', namedCurve: 'P-256', hash: 'SHA-256' };
  const keys = await globalThis.crypto.subtle.generateKey(algorithm, true, ['sign', 'verify']);
  const certificate = await X509CertificateGenerator.createSelfSigned({
    serialNumber: randomBytes(16).toString('hex'),
    name: new Name([{ CN: [name] }]),
    notBefore: new Date(Date.now() - 86_400_000),
    notAfter: new Date(Date.now() + 10 * 365 * 86_400_000),
    signingAlgorithm: algorithm,
    keys,
    extensions: [
      new BasicConstraintsExtension(false, undefined, true),
      new KeyUsagesExtension(KeyUsageFlags.digitalSignature, true),
      new ExtendedKeyUsageExtension(['1.3.6.1.5.5.7.3.1', '1.3.6.1.5.5.7.3.2']),
    ],
  });
  const key = Buffer.from(
    await globalThis.crypto.subtle.exportKey('pkcs8', keys.privateKey),
  ).toString('base64');
  return {
    version: 1,
    name,
    certificate_pem: certificate.toString('pem'),
    private_key_pem: `-----BEGIN PRIVATE KEY-----\n${key.match(/.{1,64}/g)!.join('\n')}\n-----END PRIVATE KEY-----\n`,
  };
}

export async function connectPinned(
  endpoint: string,
  pin: string,
  identity?: IdentityData,
): Promise<TLSSocket> {
  const url = new URL(canonicalEndpoint(endpoint));
  const socket = connect({
    host: url.hostname.replace(/^\[|\]$/g, ''),
    port: Number(url.port || 443),
    minVersion: 'TLSv1.3',
    maxVersion: 'TLSv1.3',
    rejectUnauthorized: false,
    cert: identity?.certificate_pem,
    key: identity?.private_key_pem,
  });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () =>
        socket.destroy(
          new SloppyError(
            'service_unavailable',
            'The Sloppy service did not complete its encrypted connection in time. Check that the service is running and reachable. Reconnecting automatically.',
          ),
        ),
      5_000,
    );
    const failed = (error: Error) => {
      clearTimeout(timer);
      socket.destroy();
      reject(error);
    };
    socket.once('error', failed);
    socket.once('secureConnect', () => {
      clearTimeout(timer);
      const raw = socket.getPeerCertificate().raw;
      if (!raw || certificateFingerprint(raw) !== pin) {
        socket.destroy();
        reject(
          new SloppyError(
            'server_identity_changed',
            'Sloppy certificate changed during connection; explicit replacement is required',
          ),
        );
      } else {
        socket.removeListener('error', failed);
        resolve(socket);
      }
    });
  });
}

export async function inspectService(endpoint: string): Promise<PresentedService> {
  endpoint = canonicalEndpoint(endpoint);
  const url = new URL(endpoint);
  const socket = connect({
    host: url.hostname.replace(/^\[|\]$/g, ''),
    port: Number(url.port || 443),
    minVersion: 'TLSv1.3',
    maxVersion: 'TLSv1.3',
    rejectUnauthorized: false,
  });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () =>
        socket.destroy(
          new SloppyError(
            'service_unavailable',
            'The Sloppy service did not provide its certificate in time. Check that the service is running and reachable. Reconnecting automatically.',
          ),
        ),
      5_000,
    );
    socket.once('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
    socket.once('secureConnect', () => {
      clearTimeout(timer);
      try {
        const raw = socket.getPeerCertificate().raw;
        if (!raw) throw new SloppyError('protocol_error', 'Service did not provide a certificate');
        const certificate = new X509Certificate(raw);
        resolve({
          endpoint,
          fingerprint: certificateFingerprint(raw),
          certificate_pem: certificate.toString(),
          name: certificate.subject,
        });
      } catch (error) {
        reject(error);
      } finally {
        socket.destroy();
      }
    });
  });
}

export class SloppyIdentity {
  readonly directory: string;
  private readonly name: string;

  constructor(options: IdentityOptions) {
    this.directory = identityDirectory(options);
    this.name = options.application;
  }

  async load(): Promise<IdentityData> {
    return locked(this.directory, async () => {
      const existing = await readOptional(join(this.directory, 'identity.json'));
      if (existing !== undefined) return identityData(existing);
      const identity = await generate(this.name);
      await privateJson(this.directory, 'identity.json', identity);
      return identity;
    });
  }

  async fingerprint(): Promise<string> {
    return certificateFingerprint(new X509Certificate((await this.load()).certificate_pem).raw);
  }

  async regenerate(): Promise<string> {
    return locked(this.directory, async () => {
      const identity = await generate(this.name);
      await privateJson(this.directory, 'identity.json', identity);
      return certificateFingerprint(new X509Certificate(identity.certificate_pem).raw);
    });
  }

  private async known(): Promise<KnownServices> {
    const value = await readOptional(join(this.directory, 'known_sloppy'));
    if (value === undefined) return { version: 1, services: {} };
    if (!value || typeof value !== 'object')
      throw new SloppyError('identity_error', 'Invalid known_sloppy');
    const data = value as KnownServices;
    if (
      data.version !== 1 ||
      !data.services ||
      typeof data.services !== 'object' ||
      Array.isArray(data.services)
    )
      throw new SloppyError('identity_error', 'Invalid known_sloppy');
    try {
      for (const [endpoint, entry] of Object.entries(data.services)) {
        if (
          !entry ||
          endpoint !== canonicalEndpoint(endpoint) ||
          entry.endpoint !== endpoint ||
          typeof entry.name !== 'string' ||
          typeof entry.certificate_pem !== 'string' ||
          certificateFingerprint(new X509Certificate(entry.certificate_pem).raw) !==
            entry.fingerprint
        )
          throw new Error('Inconsistent known_sloppy entry');
      }
    } catch (error) {
      throw new SloppyError(
        'identity_error',
        `Invalid known_sloppy: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    return data;
  }

  async listKnownServices(): Promise<KnownService[]> {
    return locked(this.directory, async () => Object.values((await this.known()).services));
  }

  async rememberService(service: PresentedService, expectedOldFingerprint?: string): Promise<void> {
    const endpoint = canonicalEndpoint(service.endpoint);
    if (
      certificateFingerprint(new X509Certificate(service.certificate_pem).raw) !==
      service.fingerprint
    )
      throw new SloppyError('invalid_input', 'Presented certificate fingerprint does not match');
    await locked(this.directory, async () => {
      const known = await this.known();
      const previous = known.services[endpoint];
      if (previous?.fingerprint !== expectedOldFingerprint)
        throw new SloppyError(
          'server_identity_changed',
          'Known service changed; explicit approval of its current fingerprint is required',
        );
      known.services[endpoint] = {
        ...service,
        endpoint,
        created_at_ms: previous?.created_at_ms ?? Date.now(),
        last_connected_at_ms: null,
      };
      await privateJson(this.directory, 'known_sloppy', known);
    });
  }

  async removeKnownService(endpoint: string, expectedFingerprint: string): Promise<void> {
    endpoint = canonicalEndpoint(endpoint);
    await locked(this.directory, async () => {
      const known = await this.known();
      if (known.services[endpoint]?.fingerprint !== expectedFingerprint)
        throw new SloppyError(
          'server_identity_changed',
          'Known service changed; fingerprint does not match',
        );
      delete known.services[endpoint];
      await privateJson(this.directory, 'known_sloppy', known);
    });
  }

  async verifyService(endpoint: string): Promise<PresentedService> {
    const presented = await inspectService(endpoint);
    const known = (await this.listKnownServices()).find(
      (entry) => entry.endpoint === presented.endpoint,
    );
    if (!known || known.fingerprint !== presented.fingerprint)
      throw new SloppyTrustRequiredError(presented, known?.fingerprint);
    return presented;
  }
}
