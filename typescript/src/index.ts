import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { Agent } from 'node:https';
import WebSocket from 'ws';
import { SessionLog, type DiagnosticDetail, type DiagnosticEvent } from './session-log.js';
import {
  SloppyError,
  connectionError,
  discoveryReadError,
  serviceMessage,
  transportCode,
} from './errors.js';
import {
  canonicalEndpoint,
  connectPinned,
  SloppyIdentity,
  SloppyTrustRequiredError,
  type PresentedService,
} from './security.js';

export { SloppyError } from './errors.js';
export { SessionLog } from './session-log.js';
export type { DiagnosticDetail, DiagnosticEvent } from './session-log.js';
export {
  SloppyIdentity,
  SloppyTrustRequiredError,
  certificateFingerprint,
  inspectService,
} from './security.js';
export type { IdentityOptions, KnownService, PresentedService } from './security.js';

export type JsonObject = Record<string, unknown>;
export interface Notification extends JsonObject {
  id: string;
  state: 'pending' | 'submitted' | 'resolved' | 'cancelled';
  requires_response: boolean;
  resolution?: { answer: JsonObject } | null;
  cancellation?: { reason: string } | null;
}
export interface ResumeCredentials {
  session_id: string;
  resume_token: string;
  instance_id: string;
}
export interface SessionOptions {
  pluginId?: string;
  pluginVersion?: string;
  application?: string;
  descriptor?: JsonObject;
  connectionFile?: string;
  endpoint?: string;
  identityDirectory?: string;
  identityProfile?: string;
  confirmServer?: (service: PresentedService) => boolean | Promise<boolean>;
  resume?: ResumeCredentials;
  retryDelayMs?: number;
  maxRetryDelayMs?: number;
}
export interface BubblePromise extends Promise<JsonObject | null> {
  readonly id: string;
  readonly created: Promise<Notification>;
}
interface Pending {
  body: JsonObject;
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  attempted: boolean;
}
interface Bubble {
  resolve: (answer: JsonObject | null) => void;
  reject: (error: Error) => void;
  attempted: boolean;
}
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TOKEN = /^[0-9a-f]{64}$/i;
const FATAL = new Set([
  'session_denied',
  'invalid_handshake',
  'invalid_plugin',
  'plugin_version_mismatch',
  'protocol_error',
  'server_trust_required',
  'server_identity_changed',
  'identity_error',
  'client_rejected',
  'unknown_client_ignored',
  'client_trust_busy',
  'client_certificate_required',
]);

function credentials(value: unknown): ResumeCredentials {
  const item = object(value);
  if (
    typeof item.session_id !== 'string' ||
    !UUID.test(item.session_id) ||
    typeof item.instance_id !== 'string' ||
    !UUID.test(item.instance_id) ||
    typeof item.resume_token !== 'string' ||
    !TOKEN.test(item.resume_token)
  ) {
    throw new SloppyError('invalid_input', 'Invalid session resume credentials');
  }
  return {
    session_id: item.session_id,
    instance_id: item.instance_id,
    resume_token: item.resume_token,
  };
}
function object(value: unknown): JsonObject {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new SloppyError('protocol_error', 'Expected a JSON object');
  }
  return value as JsonObject;
}
function notification(value: unknown, sessionId?: string): Notification {
  const item = object(value);
  if (
    typeof item.id !== 'string' ||
    !UUID.test(item.id) ||
    typeof item.requires_response !== 'boolean' ||
    !['pending', 'submitted', 'resolved', 'cancelled'].includes(String(item.state))
  ) {
    throw new SloppyError('protocol_error', 'Invalid bubble notification');
  }
  if (sessionId && item.session_id !== sessionId) {
    throw new SloppyError('protocol_error', 'Notification belongs to another session');
  }
  if (
    item.resolution != null ||
    (item.requires_response && ['submitted', 'resolved'].includes(String(item.state)))
  ) {
    object(object(item.resolution).answer);
  }
  if (item.cancellation != null && typeof object(item.cancellation).reason !== 'string') {
    throw new SloppyError('protocol_error', 'Invalid bubble cancellation');
  }
  return item as Notification;
}
function encode(value: unknown): string {
  try {
    return JSON.stringify(value, (_key, item: unknown) => {
      if (typeof item === 'number' && !Number.isFinite(item)) {
        throw new SloppyError('invalid_input', 'Non-finite JSON numbers are not supported');
      }
      if (['undefined', 'function', 'symbol', 'bigint'].includes(typeof item)) {
        throw new SloppyError('invalid_input', 'Values must be representable as JSON');
      }
      return item;
    });
  } catch (error) {
    if (error instanceof SloppyError) throw error;
    throw new SloppyError('invalid_input', error instanceof Error ? error.message : String(error));
  }
}
export function connectionPath(): string {
  if (process.env.SLOPPY_CONNECTION_FILE) return process.env.SLOPPY_CONNECTION_FILE;
  if (process.platform === 'darwin')
    return join(homedir(), 'Library/Application Support/Sloppy/connection.json');
  if (process.platform === 'win32') {
    if (!process.env.APPDATA) throw new SloppyError('discovery_error', 'APPDATA is not set');
    return join(process.env.APPDATA, 'Sloppy/connection.json');
  }
  return join(
    process.env.XDG_DATA_HOME ?? join(homedir(), '.local/share'),
    'sloppy/connection.json',
  );
}
export class SloppySession extends EventEmitter {
  private socket?: WebSocket;
  private timer?: ReturnType<typeof setTimeout>;
  private watchdog?: ReturnType<typeof setInterval>;
  private stopped = false;
  private retry: number;
  private pending = new Map<string, Pending>();
  private bubbles = new Map<string, Bubble>();
  private readyResolve!: () => void;
  private readyReject!: (error: Error) => void;
  private expectedInstance?: string;
  private lastMessage = 0;
  private handshakeDeadline = 0;
  private trustReady = false;
  private trustPending = false;
  private heartbeatTimeout = 15_000;
  private options: SessionOptions;
  readonly ready: Promise<void>;
  readonly identity: SloppyIdentity;
  online = false;
  lastError: string | null = null;
  private savedCredentials?: ResumeCredentials;
  private readonly diagnosticId = randomUUID();
  private readonly log: SessionLog;
  private attempts = 0;
  private initialDiagnostics: { event: DiagnosticEvent; detail: DiagnosticDetail }[] = [];
  diagnosticError: SloppyError | null = null;
  get diagnosticFile(): string {
    return this.log.path(this.sessionId ?? this.diagnosticId);
  }
  get resumeCredentials(): ResumeCredentials | undefined {
    return this.savedCredentials ? { ...this.savedCredentials } : undefined;
  }
  get sessionId(): string | undefined {
    return this.resumeCredentials?.session_id;
  }
  get instanceId(): string | undefined {
    return this.resumeCredentials?.instance_id;
  }

  constructor(options: SessionOptions = {}) {
    super();
    this.options = {
      ...options,
      pluginId: options.pluginId ?? process.env.SLOPPY_PLUGIN_ID,
      pluginVersion: options.pluginVersion ?? process.env.SLOPPY_PLUGIN_VERSION,
      descriptor: options.descriptor ? object(JSON.parse(encode(options.descriptor))) : undefined,
    };
    if (!this.options.pluginId?.trim() || this.options.pluginId.length > 256)
      throw new SloppyError('invalid_input', 'Specify pluginId or launch with sloppy');
    if (
      !Number.isFinite(options.retryDelayMs ?? 500) ||
      (options.retryDelayMs ?? 500) <= 0 ||
      !Number.isFinite(options.maxRetryDelayMs ?? 30_000) ||
      (options.maxRetryDelayMs ?? 30_000) < (options.retryDelayMs ?? 500)
    ) {
      throw new SloppyError('invalid_input', 'Retry delays must be positive and bounded');
    }
    this.savedCredentials = options.resume ? credentials(options.resume) : undefined;
    this.identity = new SloppyIdentity({
      application: this.options.application ?? this.options.pluginId,
      pluginId: this.options.pluginId,
      directory: options.identityDirectory,
      profile: options.identityProfile,
    });
    this.log = new SessionLog(
      join(dirname(options.connectionFile ?? connectionPath()), 'logs', 'client-sessions'),
      this.diagnosticId,
    );
    this.retry = options.retryDelayMs ?? 500;
    this.ready = new Promise((resolve, reject) => {
      this.readyResolve = resolve;
      this.readyReject = reject;
    });
    // Operations and status events carry errors too; unused readiness must not cause an unhandled rejection.
    this.ready.catch(() => {});
    this.schedule(0);
  }

  private diagnostic(event: DiagnosticEvent, detail: DiagnosticDetail = {}): void {
    const metadata = { attempt: this.attempts, ...detail };
    if (!this.sessionId) {
      this.initialDiagnostics.push({ event, detail: metadata });
      if (this.initialDiagnostics.length > 64) this.initialDiagnostics.shift();
    }
    try {
      if (this.log.record(this.sessionId ?? this.diagnosticId, event, metadata))
        this.diagnosticError = null;
    } catch {
      // Filesystem messages can contain sensitive paths; expose a fixed, actionable failure.
      this.diagnosticError = new SloppyError(
        'diagnostic_error',
        'Cannot write private Sloppy session log',
      );
      this.emit('diagnostic_error', this.diagnosticError);
    }
  }

  private status(online: boolean, error: string | null, code?: string): void {
    this.online = online;
    this.lastError = error;
    this.emit('status', { online, error, code });
  }
  private schedule(delay: number): void {
    if (!this.stopped) {
      this.diagnostic('retry_scheduled', { delay_ms: delay });
      this.timer = setTimeout(() => {
        this.timer = undefined;
        void this.connect();
      }, delay);
    }
  }
  private fail(error: SloppyError): void {
    this.stopped = true;
    clearTimeout(this.timer);
    clearInterval(this.watchdog);
    this.readyReject(error);
    for (const request of this.pending.values()) request.reject(error);
    for (const bubble of this.bubbles.values()) bubble.reject(error);
    this.pending.clear();
    this.bubbles.clear();
    this.status(false, error.message, error.code);
  }
  private loseSession(): void {
    this.diagnostic('session_lost');
    const error = new SloppyError(
      'session_lost',
      'Sloppy restarted or no longer retains this session; old bubbles were not replayed',
    );
    this.savedCredentials = undefined;
    for (const [id, request] of this.pending) {
      if (request.attempted) {
        request.reject(error);
        this.pending.delete(id);
      }
    }
    for (const [id, bubble] of this.bubbles) {
      if (bubble.attempted) {
        bubble.reject(error);
        this.bubbles.delete(id);
      }
    }
    this.emit('session_lost', error);
  }
  private async connect(): Promise<void> {
    this.diagnostic('connect_attempt', {
      attempt: ++this.attempts,
      resumed: !!this.resumeCredentials,
      pending_request_count: this.pending.size,
    });
    try {
      const endpoint = this.options.endpoint ?? process.env.SLOPPY_SERVICE_URL;
      let connection: JsonObject;
      if (endpoint) {
        connection = { base_url: canonicalEndpoint(endpoint) };
      } else {
        const file = this.options.connectionFile ?? connectionPath();
        let contents: string;
        try {
          contents = await readFile(file, 'utf8');
        } catch (error) {
          throw discoveryReadError(error, file);
        }
        try {
          connection = object(JSON.parse(contents));
        } catch {
          throw new SloppyError(
            'discovery_error',
            'The Sloppy connection file is not valid service discovery JSON. Restart Sloppy to recreate it, or check the configured connection file.',
          );
        }
        if (
          connection.protocol_version !== 1 ||
          connection.service !== 'SloppyService' ||
          !TOKEN.test(String(connection.token)) ||
          !UUID.test(String(connection.instance_id))
        )
          throw new SloppyError(
            'discovery_error',
            'The Sloppy connection file has invalid or unsupported service metadata. Restart Sloppy to recreate it and check that the SDK and Sloppy versions are compatible.',
          );
        try {
          connection.base_url = canonicalEndpoint(String(connection.base_url));
        } catch {
          throw new SloppyError(
            'discovery_error',
            'The Sloppy connection file has an invalid HTTPS service address. Restart Sloppy to recreate it, or check the configured connection file.',
          );
        }
      }
      if (this.stopped) return;
      const baseUrl = canonicalEndpoint(String(connection.base_url));
      const identity = await this.identity.load();
      let service: PresentedService;
      try {
        service = await this.identity.verifyService(baseUrl);
      } catch (error) {
        if (
          !(error instanceof SloppyTrustRequiredError) ||
          error.knownFingerprint ||
          !this.options.confirmServer ||
          (await this.options.confirmServer(error.presented)) !== true
        )
          throw error;
        if (this.stopped) return;
        await this.identity.rememberService(error.presented);
        service = error.presented;
      }
      if (
        this.resumeCredentials &&
        connection.instance_id &&
        this.resumeCredentials.instance_id !== connection.instance_id
      )
        this.loseSession();
      this.expectedInstance =
        typeof connection.instance_id === 'string' ? connection.instance_id : undefined;
      if (!this.options.descriptor && process.env.SLOPPY_PLUGIN_MANIFEST) {
        const manifest = object(
          JSON.parse(await readFile(process.env.SLOPPY_PLUGIN_MANIFEST, 'utf8')),
        );
        if (manifest.manifest_version !== 1)
          throw new SloppyError('invalid_plugin', 'Unsupported plugin manifest version');
        this.options.descriptor = object(manifest.plugin);
      }
      const stream = await connectPinned(baseUrl, service.fingerprint, identity);
      if (this.stopped) {
        stream.destroy();
        return;
      }
      const agent = new Agent({ keepAlive: false });
      agent.createConnection = () => stream;
      const socket = new WebSocket(baseUrl.replace('https:', 'wss:') + '/v1/sessions', {
        headers: connection.token ? { Authorization: 'Bearer ' + String(connection.token) } : {},
        agent,
        followRedirects: false,
        perMessageDeflate: false,
        handshakeTimeout: 3_000,
        maxPayload: 2 * 1024 * 1024,
      });
      this.socket = socket;
      let ended = false;
      const disconnected = (error: string, fatal?: SloppyError, code = 'service_unavailable') => {
        if (ended || this.socket !== socket) return;
        ended = true;
        this.socket = undefined;
        clearInterval(this.watchdog);
        socket.terminate();
        if (this.stopped) return;
        this.diagnostic('disconnected', { code: fatal ? 'protocol_error' : code });
        if (fatal) {
          this.fail(fatal);
          return;
        }
        this.status(false, error, code);
        const delay = this.retry;
        this.retry = Math.min(this.retry * 2, this.options.maxRetryDelayMs ?? 30_000);
        this.schedule(delay);
      };
      socket.on('open', () => {
        if (this.stopped || this.socket !== socket) {
          socket.terminate();
          return;
        }
        this.lastMessage = Date.now();
        this.handshakeDeadline = this.lastMessage + 5_000;
        this.trustReady = false;
        this.trustPending = false;
        this.diagnostic('socket_open', { resumed: !!this.resumeCredentials });
        this.watchdog = setInterval(() => {
          if (!this.online && Date.now() >= this.handshakeDeadline) {
            this.diagnostic('handshake_timeout');
            disconnected('Sloppy session handshake timed out');
          } else if (this.online && Date.now() - this.lastMessage >= this.heartbeatTimeout) {
            this.diagnostic('heartbeat_timeout');
            disconnected('Sloppy connection heartbeat timed out');
          }
        }, 250);
      });
      socket.on('message', (data) => {
        if (this.socket !== socket || this.stopped) return;
        try {
          const body = object(JSON.parse(data.toString()));
          const frameType =
            body.type === 'ready' || body.type === 'reply' || body.type === 'notification'
              ? body.type
              : 'other';
          if (body.type !== 'ping')
            this.diagnostic('received', {
              frame_type: frameType,
              bytes: Buffer.byteLength(data.toString()),
              notification_count: Array.isArray(body.notifications)
                ? body.notifications.length
                : undefined,
            });
          this.receive(body);
        } catch (error) {
          const failure =
            error instanceof SloppyError
              ? error
              : new SloppyError(
                  'protocol_error',
                  error instanceof Error ? error.message : String(error),
                );
          this.diagnostic('protocol_error');
          disconnected(failure.message, FATAL.has(failure.code) ? failure : undefined);
        }
      });
      socket.on('error', (error) => {
        this.diagnostic('transport_error', {
          code: transportCode(error),
        });
        const failure = connectionError(error) as Error;
        const code =
          failure instanceof SloppyError
            ? failure.code
            : 'code' in error &&
                [
                  'ECONNREFUSED',
                  'ECONNRESET',
                  'EPIPE',
                  'ETIMEDOUT',
                  'ENETUNREACH',
                  'EHOSTUNREACH',
                ].includes(String(error.code))
              ? 'service_unavailable'
              : 'connection_error';
        disconnected(
          failure.message,
          error instanceof SloppyError && FATAL.has(error.code) ? error : undefined,
          code,
        );
      });
      socket.on('close', (code, reason) => {
        agent.destroy();
        this.diagnostic('socket_closed', { close_code: code });
        disconnected('Sloppy connection closed' + (reason.length ? ': ' + reason.toString() : ''));
      });
    } catch (cause) {
      if (this.stopped) return;
      const transport = transportCode(cause);
      if (transport !== 'other') this.diagnostic('transport_error', { code: transport });
      const error = connectionError(cause);
      if (error instanceof SloppyError && FATAL.has(error.code)) {
        this.fail(error);
        return;
      }
      this.diagnostic(
        error instanceof SloppyError && error.code === 'service_unavailable'
          ? 'disconnected'
          : 'discovery_error',
        {
          code:
            error instanceof SloppyError &&
            ['service_unavailable', 'discovery_error'].includes(error.code)
              ? error.code
              : 'connection_error',
        },
      );
      this.status(
        false,
        error instanceof Error ? error.message : String(error),
        error instanceof SloppyError ? error.code : undefined,
      );
      const delay = this.retry;
      this.retry = Math.min(this.retry * 2, this.options.maxRetryDelayMs ?? 30_000);
      this.schedule(delay);
    }
  }
  private receive(body: JsonObject): void {
    this.lastMessage = Date.now();
    if (body.type === 'trust_pending') {
      if (
        this.trustReady ||
        this.trustPending ||
        this.online ||
        typeof body.timeout_ms !== 'number' ||
        !Number.isFinite(body.timeout_ms) ||
        body.timeout_ms <= 0 ||
        body.timeout_ms > 120_000
      )
        throw new SloppyError('protocol_error', 'Invalid client trust negotiation');
      this.trustPending = true;
      this.handshakeDeadline = Date.now() + body.timeout_ms + 1_000;
      this.status(false, 'Waiting for client certificate approval in Sloppy', 'trust_pending');
      return;
    }
    if (body.type === 'trust_ready') {
      if (!this.options.pluginVersion && typeof this.options.descriptor?.version === 'string')
        this.options.pluginVersion = this.options.descriptor.version;
      if (this.trustReady || this.online || body.protocol_version !== 1)
        throw new SloppyError('protocol_error', 'Invalid client trust confirmation');
      this.trustReady = true;
      this.handshakeDeadline = Date.now() + 5_000;
      this.socket?.send(
        encode({
          type: 'hello',
          protocol_version: 1,
          plugin_id: this.options.pluginId,
          ...(this.options.pluginVersion ? { plugin_version: this.options.pluginVersion } : {}),
          application: this.options.application ?? this.options.pluginId,
          descriptor: this.options.descriptor ?? null,
          resume: this.resumeCredentials ?? null,
        }),
      );
      return;
    }
    if (body.type === 'ping') {
      this.socket?.send(encode({ type: 'pong' }));
      return;
    }
    if (body.type === 'error') {
      if (body.code === 'session_unknown') this.loseSession();
      throw new SloppyError(
        String(body.code),
        serviceMessage(
          String(body.code),
          String(body.message),
          this.options.pluginId +
            (this.options.pluginVersion ? '@' + this.options.pluginVersion : ''),
        ),
      );
    }
    if (body.type === 'ready') {
      if (
        body.protocol_version !== 1 ||
        typeof body.instance_id !== 'string' ||
        !UUID.test(body.instance_id) ||
        (this.expectedInstance !== undefined && body.instance_id !== this.expectedInstance) ||
        !this.trustReady ||
        typeof body.session_id !== 'string' ||
        !UUID.test(body.session_id) ||
        typeof body.resume_token !== 'string' ||
        !TOKEN.test(body.resume_token) ||
        typeof body.heartbeat_timeout_ms !== 'number' ||
        !Number.isFinite(body.heartbeat_timeout_ms) ||
        body.heartbeat_timeout_ms < 50 ||
        body.heartbeat_timeout_ms > 120_000 ||
        typeof body.heartbeat_interval_ms !== 'number' ||
        body.heartbeat_interval_ms <= 0 ||
        body.heartbeat_interval_ms >= body.heartbeat_timeout_ms ||
        this.online ||
        !Array.isArray(body.notifications)
      )
        throw new SloppyError('protocol_error', 'Invalid session handshake');
      const sessionId = body.session_id;
      if (typeof body.plugin_version === 'string') this.options.pluginVersion = body.plugin_version;
      const snapshot = body.notifications.map((item) => notification(item, sessionId));
      const replyIds = body.pending_reply_ids ?? [];
      if (
        !Array.isArray(replyIds) ||
        replyIds.some((id) => typeof id !== 'string' || !UUID.test(id))
      ) {
        throw new SloppyError('protocol_error', 'Invalid pending reply snapshot');
      }
      this.savedCredentials = {
        session_id: body.session_id,
        resume_token: body.resume_token,
        instance_id: body.instance_id,
      };
      for (const { event, detail } of this.initialDiagnostics) this.diagnostic(event, detail);
      this.initialDiagnostics = [];
      this.diagnostic('ready', {
        resumed: body.resumed === true,
        notification_count: snapshot.length,
        pending_reply_count: replyIds.length,
      });
      this.heartbeatTimeout = body.heartbeat_timeout_ms;
      this.retry = this.options.retryDelayMs ?? 500;
      this.status(true, null);
      for (const item of snapshot) this.update(item);
      this.readyResolve();
      this.emit('ready', this.resumeCredentials);
      for (const id of replyIds) {
        if (!this.pending.has(id)) this.socket?.send(encode({ type: 'received', request_id: id }));
      }
      for (const request of this.pending.values()) this.transmit(request);
      return;
    }
    if (!this.online) throw new SloppyError('protocol_error', 'Expected a session handshake first');
    if (body.type === 'notification') {
      this.update(notification(body.notification));
      return;
    }
    if (
      body.type !== 'reply' ||
      typeof body.request_id !== 'string' ||
      typeof body.ok !== 'boolean'
    ) {
      throw new SloppyError('protocol_error', 'Unknown session message');
    }
    const request = this.pending.get(body.request_id);
    if (request) {
      if (body.ok) {
        if (request.body.op === 'list') {
          if (!Array.isArray(body.value))
            throw new SloppyError('protocol_error', 'Expected a bubble list');
          request.resolve(body.value.map((item) => notification(item, this.sessionId)));
        } else {
          request.resolve(notification(body.value, this.sessionId));
        }
      } else
        request.reject(
          new SloppyError(
            'request_failed',
            String(body.error),
            typeof body.status === 'number' ? body.status : undefined,
          ),
        );
      this.pending.delete(body.request_id);
    }
    this.socket?.send(encode({ type: 'received', request_id: body.request_id }));
  }
  private update(item: Notification, emit = true): void {
    if (item.session_id !== this.sessionId) {
      throw new SloppyError('protocol_error', 'Notification belongs to another session');
    }
    const bubble = this.bubbles.get(item.id);
    if (bubble) {
      if (item.state === 'submitted' || item.state === 'resolved') {
        if (item.resolution) {
          bubble.resolve(structuredClone(item.resolution.answer));
          this.bubbles.delete(item.id);
        }
      } else if (item.state === 'cancelled') {
        bubble.reject(
          new SloppyError(
            'dismissed',
            item.cancellation?.reason ?? 'Bubble dismissed without an answer',
          ),
        );
      }
      if (item.state === 'resolved' || item.state === 'cancelled') this.bubbles.delete(item.id);
    }
    if (emit) this.emit('notification', item);
  }
  private transmit(request: Pending): void {
    if (!this.online || this.socket?.readyState !== WebSocket.OPEN) return;
    request.attempted = true;
    const bubble = this.bubbles.get(String(request.body.id));
    if (bubble) bubble.attempted = true;
    const text = encode(request.body);
    this.diagnostic('sending', { frame_type: 'request', bytes: Buffer.byteLength(text) });
    this.socket.send(text);
  }
  private request(op: string, body: JsonObject = {}): Promise<unknown> {
    if (this.stopped) return Promise.reject(new SloppyError('closed', 'Sloppy session is closed'));
    if (this.pending.size >= 128)
      return Promise.reject(new SloppyError('queue_full', 'Too many pending session operations'));
    let message: JsonObject;
    try {
      const serialized = encode({ ...body, type: 'request', request_id: randomUUID(), op });
      if (Buffer.byteLength(serialized) > 2 * 1024 * 1024) {
        throw new SloppyError('invalid_input', 'Session requests must not exceed 2 MiB');
      }
      message = object(JSON.parse(serialized));
    } catch (error) {
      return Promise.reject(error);
    }
    return new Promise((resolve, reject) => {
      const request = { body: message, resolve, reject, attempted: false };
      this.pending.set(String(message.request_id), request);
      this.transmit(request);
    });
  }
  send(content: JsonObject): BubblePromise {
    const id = randomUUID();
    const response = new Promise<JsonObject | null>((resolve, reject) =>
      this.bubbles.set(id, { resolve, reject, attempted: false }),
    );
    response.catch(() => {});
    const created = this.request('create', {
      id,
      notification: {
        ...content,
        origin: content.origin ?? {
          application: this.options.application ?? this.options.pluginId,
          plugin: this.options.pluginId,
          instance: 'sdk',
        },
      },
    }).then(notification);
    created.then(
      (item) => {
        if (!item.requires_response) {
          this.bubbles.get(id)?.resolve(null);
          this.bubbles.delete(id);
        }
        this.update(item, false);
      },
      (error: Error) => {
        this.bubbles.get(id)?.reject(error);
        this.bubbles.delete(id);
      },
    );
    return Object.assign(response, { id, created });
  }
  async list(): Promise<Notification[]> {
    const value = await this.request('list');
    if (!Array.isArray(value)) throw new SloppyError('protocol_error', 'Expected a bubble list');
    return value.map((item) => notification(item, this.sessionId));
  }
  async get(id: string): Promise<Notification> {
    return notification(await this.request('get', { id }));
  }
  async answer(id: string, answer: JsonObject): Promise<Notification> {
    return notification(
      await this.request('answer', {
        id,
        answer: { ...answer, source: this.options.application ?? this.options.pluginId },
      }),
    );
  }
  async resolve(id: string, answer: JsonObject): Promise<Notification> {
    return notification(
      await this.request('resolve', {
        id,
        answer: { ...answer, source: this.options.application ?? this.options.pluginId },
      }),
    );
  }
  async acknowledge(
    id: string,
    status: 'accepted' | 'rejected',
    message: string | null = null,
  ): Promise<Notification> {
    return notification(
      await this.request('acknowledge', {
        id,
        acknowledgement: { source: 'sdk', status, message },
      }),
    );
  }
  async dismiss(
    id: string,
    reason = 'Dismissed by originating application',
  ): Promise<Notification> {
    return notification(await this.request('dismiss', { id, reason }));
  }
  async regenerateCertificate(): Promise<string> {
    this.close();
    return this.identity.regenerate();
  }
  close(): void {
    if (this.stopped) return;
    this.diagnostic('closed');
    this.stopped = true;
    clearTimeout(this.timer);
    clearInterval(this.watchdog);
    const socket = this.socket;
    this.socket = undefined;
    if (socket?.readyState === WebSocket.OPEN) socket.close(1000, 'Plugin session closed');
    else socket?.terminate();
    const error = new SloppyError('closed', 'Sloppy session closed');
    this.readyReject(error);
    for (const request of this.pending.values()) request.reject(error);
    for (const bubble of this.bubbles.values()) bubble.reject(error);
    this.pending.clear();
    this.bubbles.clear();
    this.status(false, null);
  }
}
