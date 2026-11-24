import errno
import socket
import ssl


class SloppyError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


LOCAL_SERVICE_HELP = 'Local Sloppy service is unavailable. Open Sloppy and turn on "Enable Sloppy service server" in Settings.'


def discovery_read_error(error, file):
    if isinstance(error, FileNotFoundError):
        return SloppyError(
            "service_unavailable",
            f"{LOCAL_SERVICE_HELP} No connection file was found at {file}. If you configured a custom connection file, check its path.",
        )
    detail = ": permission denied" if isinstance(error, PermissionError) else ""
    return SloppyError(
        "discovery_error",
        f"Cannot read the Sloppy connection file at {file}{detail}. Check the file's permissions and the configured connection path.",
    )


def connection_error(error):
    if isinstance(error, SloppyError):
        return error
    if isinstance(error, ssl.SSLError):
        return SloppyError(
            "connection_error",
            "Cannot establish an encrypted connection to Sloppy. Check that the address points to a Sloppy HTTPS service and that the plugin and service versions are compatible. Reconnecting automatically.",
        )
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status == 401:
        return SloppyError(
            "connection_error",
            "Sloppy rejected the connection credentials (HTTP 401). If using a connection file, restart Sloppy to refresh it; otherwise review the client identity and service permissions.",
        )
    if status == 403:
        return SloppyError(
            "connection_error",
            "Sloppy denied this connection (HTTP 403). Review client approval and remote-client settings in Sloppy before retrying.",
        )
    if isinstance(error, socket.gaierror):
        return SloppyError(
            "service_unavailable",
            "Cannot find the Sloppy service host. Check its hostname and your network or VPN connection. Reconnecting automatically.",
        )
    if (
        isinstance(error, (ConnectionError, TimeoutError))
        or isinstance(error, OSError)
        and error.errno
        in {
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.EPIPE,
            errno.ETIMEDOUT,
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
        }
    ):
        return SloppyError(
            "service_unavailable",
            "Cannot reach the Sloppy service. Check that its server is running and the configured address is reachable. For a local service, open Sloppy Settings and enable its service server. Reconnecting automatically.",
        )
    return error


def service_message(code, fallback, plugin_id=None):
    messages = {
        "plugin_disabled": f"This plugin's Sloppy integration is disabled. With the service running, run `sloppy plugins enable {plugin_id or 'PLUGIN_ID'}` to enable it. Reconnecting automatically.",
        "service_stopped": "The Sloppy service has stopped. Restart the service to reconnect. For a local service, open Sloppy Settings and enable its service server.",
        "client_rejected": "Sloppy rejected this client. Open Sessions in Sloppy and review its Rejected clients, then restart this plugin.",
        "unknown_client_ignored": 'Sloppy is ignoring unknown clients. In Sloppy Settings, turn off "Ignore unknown clients" or add this client, then restart this plugin.',
        "client_trust_busy": "Sloppy has too many client approval requests waiting. Resolve the pending approvals in Sloppy, then restart this plugin.",
        "client_certificate_required": "Sloppy requires a client certificate. Update this plugin and its Sloppy SDK, then reconnect.",
        "session_denied": "Sloppy could not resume this session with the supplied credentials. Restart this plugin to create a new session.",
    }
    return messages.get(code, fallback)
