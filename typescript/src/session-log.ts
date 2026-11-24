import {
  appendFileSync,
  chmodSync,
  closeSync,
  constants,
  fchmodSync,
  fstatSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
} from 'node:fs';
import { join } from 'node:path';

const MAX_BYTES = 1024 * 1024;
type LogLevel = 'error' | 'info' | 'debug';
const RANK = { error: 0, info: 1, debug: 2 };

function eventLevel(event: DiagnosticEvent, detail: DiagnosticDetail): LogLevel {
  if (event === 'sending' || event === 'received') return 'debug';
  if (event === 'socket_closed' && detail.close_code !== 1000 && detail.close_code !== 1001)
    return 'error';
  if (
    [
      'transport_error',
      'protocol_error',
      'discovery_error',
      'adapter_error',
      'handshake_timeout',
      'heartbeat_timeout',
    ].includes(event)
  )
    return 'error';
  return 'info';
}
export type DiagnosticEvent =
  | 'connect_attempt'
  | 'retry_scheduled'
  | 'socket_open'
  | 'ready'
  | 'session_lost'
  | 'transport_error'
  | 'socket_closed'
  | 'handshake_timeout'
  | 'heartbeat_timeout'
  | 'protocol_error'
  | 'discovery_error'
  | 'adapter_error'
  | 'disconnected'
  | 'closed'
  | 'received'
  | 'sending';
export interface DiagnosticDetail {
  attempt?: number;
  resumed?: boolean;
  delay_ms?: number;
  bytes?: number;
  frame_type?: 'ready' | 'reply' | 'notification' | 'request' | 'hello' | 'other';
  notification_count?: number;
  pending_reply_count?: number;
  pending_request_count?: number;
  close_code?: number;
  code?: string;
  component?: 'copilot';
  operation?:
    | 'bridge'
    | 'bridge_connection'
    | 'permission_scope'
    | 'notification_hook'
    | 'notification_relay'
    | 'extension'
    | 'launcher';
}

export class SessionLog {
  constructor(
    private readonly directory: string,
    private readonly clientId: string,
  ) {}

  path(id: string): string {
    return join(this.directory, `${id}.log`);
  }

  private verbosity(): LogLevel {
    let fd: number;
    try {
      fd = openSync(
        join(this.directory, '..', 'verbosity.json'),
        constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0),
      );
    } catch (error) {
      if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return 'info';
      throw error;
    }
    try {
      const metadata = fstatSync(fd);
      if (!metadata.isFile() || metadata.size > 4096)
        throw new Error('Invalid private log verbosity file');
      const value: unknown = JSON.parse(readFileSync(fd, 'utf8'));
      if (
        !value ||
        typeof value !== 'object' ||
        !('level' in value) ||
        (value.level !== 'error' && value.level !== 'info' && value.level !== 'debug')
      ) {
        throw new Error('Invalid log verbosity');
      }
      return value.level;
    } finally {
      closeSync(fd);
    }
  }

  record(id: string, event: DiagnosticEvent, detail: DiagnosticDetail = {}): boolean {
    // IDs originate only from local UUIDs or validated session credentials.
    for (const directory of [join(this.directory, '..'), this.directory]) {
      try {
        mkdirSync(directory, { mode: 0o700 });
      } catch (error) {
        if (!(error instanceof Error && 'code' in error && error.code === 'EEXIST')) throw error;
      }
      const metadata = lstatSync(directory);
      if (!metadata.isDirectory() || metadata.isSymbolicLink())
        throw new Error('Session log directory must be a regular directory');
      if (process.platform !== 'win32') chmodSync(directory, 0o700);
    }
    const level = eventLevel(event, detail);
    if (RANK[level] > RANK[this.verbosity()]) return false;
    const line =
      JSON.stringify({
        timestamp_ms: Date.now(),
        source: 'sdk',
        client_id: this.clientId,
        session_id: id,
        event,
        level,
        detail,
      }) + '\n';
    const path = this.path(id);
    let fd = openSync(
      path,
      constants.O_WRONLY |
        constants.O_APPEND |
        constants.O_CREAT |
        (constants.O_NONBLOCK ?? 0) |
        (constants.O_NOFOLLOW ?? 0),
      0o600,
    );
    try {
      const metadata = fstatSync(fd);
      if (!metadata.isFile() || metadata.nlink !== 1)
        throw new Error('Session log must be a regular private file with no hard links');
      if (process.platform !== 'win32') fchmodSync(fd, 0o600);
      if (metadata.size + Buffer.byteLength(line) > MAX_BYTES) {
        closeSync(fd);
        fd = -1;
        const previous = path + '.1';
        try {
          const prior = lstatSync(previous);
          if (!prior.isFile() || prior.isSymbolicLink())
            throw new Error('Rotated session log must be a regular file');
        } catch (error) {
          if (!(error instanceof Error && 'code' in error && error.code === 'ENOENT')) throw error;
        }
        renameSync(path, previous);
        fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
      }
      appendFileSync(fd, line);
    } finally {
      if (fd !== -1) closeSync(fd);
    }
    return true;
  }
}
