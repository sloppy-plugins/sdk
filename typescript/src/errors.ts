export class SloppyError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = 'SloppyError';
  }
}

export const localServiceHelp =
  'Local Sloppy service is unavailable. Open Sloppy and turn on "Enable Sloppy service server" in Settings.';

export function transportCode(error: unknown): string {
  const code = error instanceof Error && 'code' in error ? String(error.code) : '';
  return [
    'ECONNREFUSED',
    'ECONNRESET',
    'EPIPE',
    'ETIMEDOUT',
    'ENETUNREACH',
    'EHOSTUNREACH',
    'ENOTFOUND',
    'EAI_AGAIN',
    'EPROTO',
    'WS_ERR_UNSUPPORTED_MESSAGE_LENGTH',
    'WS_ERR_INVALID_UTF8',
    'WS_ERR_UNEXPECTED_RSV_1',
  ].includes(code)
    ? code
    : 'other';
}

export function discoveryReadError(error: unknown, file: string): SloppyError {
  const code = error instanceof Error && 'code' in error ? error.code : undefined;
  if (code === 'ENOENT')
    return new SloppyError(
      'service_unavailable',
      `${localServiceHelp} No connection file was found at ${file}. If you configured a custom connection file, check its path.`,
    );
  return new SloppyError(
    'discovery_error',
    `Cannot read the Sloppy connection file at ${file}${code === 'EACCES' || code === 'EPERM' ? ': permission denied' : ''}. Check the file's permissions and the configured connection path.`,
  );
}

export function connectionError(error: unknown): unknown {
  if (error instanceof SloppyError) return error;
  const code = error instanceof Error && 'code' in error ? error.code : undefined;
  if (
    String(code).startsWith('ERR_SSL_') ||
    String(code).startsWith('ERR_TLS_') ||
    code === 'EPROTO'
  )
    return new SloppyError(
      'connection_error',
      'Cannot establish an encrypted connection to Sloppy. Check that the address points to a Sloppy HTTPS service and that the plugin and service versions are compatible. Reconnecting automatically.',
    );
  const status =
    error instanceof Error
      ? /^Unexpected server response: (\d{3})$/.exec(error.message)?.[1]
      : undefined;
  if (status === '401')
    return new SloppyError(
      'connection_error',
      'Sloppy rejected the connection credentials (HTTP 401). If using a connection file, restart Sloppy to refresh it; otherwise review the client identity and service permissions.',
    );
  if (status === '403')
    return new SloppyError(
      'connection_error',
      'Sloppy denied this connection (HTTP 403). Review client approval and remote-client settings in Sloppy before retrying.',
    );
  if (code === 'ENOTFOUND' || code === 'EAI_AGAIN')
    return new SloppyError(
      'service_unavailable',
      'Cannot find the Sloppy service host. Check its hostname and your network or VPN connection. Reconnecting automatically.',
    );
  if (
    ['ECONNREFUSED', 'ECONNRESET', 'EPIPE', 'ETIMEDOUT', 'ENETUNREACH', 'EHOSTUNREACH'].includes(
      String(code),
    )
  )
    return new SloppyError(
      'service_unavailable',
      'Cannot reach the Sloppy service. Check that its server is running and the configured address is reachable. For a local service, open Sloppy Settings and enable its service server. Reconnecting automatically.',
    );
  return error;
}

export function serviceMessage(code: string, fallback: string, pluginId?: string): string {
  const messages: Record<string, string> = {
    plugin_disabled: `This plugin's Sloppy integration is disabled. With the service running, run \`sloppy plugins enable ${pluginId ?? 'PLUGIN_ID'}\` to enable it. Reconnecting automatically.`,
    service_stopped:
      'The Sloppy service has stopped. Restart the service to reconnect. For a local service, open Sloppy Settings and enable its service server.',
    client_rejected:
      'Sloppy rejected this client. Open Sessions in Sloppy and review its Rejected clients, then restart this plugin.',
    unknown_client_ignored:
      'Sloppy is ignoring unknown clients. In Sloppy Settings, turn off "Ignore unknown clients" or add this client, then restart this plugin.',
    client_trust_busy:
      'Sloppy has too many client approval requests waiting. Resolve the pending approvals in Sloppy, then restart this plugin.',
    client_certificate_required:
      'Sloppy requires a client certificate. Update this plugin and its Sloppy SDK, then reconnect.',
    session_denied:
      'Sloppy could not resume this session with the supplied credentials. Restart this plugin to create a new session.',
  };
  return messages[code] ?? fallback;
}
