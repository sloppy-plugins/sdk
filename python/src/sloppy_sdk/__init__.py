"""Owned, resumable Sloppy sessions; transport and recovery stay inside the SDK."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import Future, InvalidStateError, ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from ._errors import (
    SloppyError,
    connection_error,
    discovery_read_error,
    service_message,
)
from ._session_log import SessionLog
from .security import (
    PresentedService,
    SloppyIdentity,
    SloppyTrustRequiredError,
    canonical_endpoint,
    certificate_fingerprint,
    inspect_service,
)

__all__ = [
    "SloppySession",
    "SloppyActionsProvider",
    "ActionContext",
    "SloppyError",
    "BubbleFuture",
    "connection_path",
    "discover",
    "SloppyIdentity",
    "SloppyTrustRequiredError",
    "PresentedService",
    "certificate_fingerprint",
    "inspect_service",
]
_LOG = logging.getLogger(__name__)
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_TOKEN = re.compile(r"[0-9a-fA-F]{64}")
_FATAL = {
    "session_denied",
    "invalid_handshake",
    "invalid_plugin",
    "plugin_version_mismatch",
    "protocol_error",
    "server_trust_required",
    "server_identity_changed",
    "identity_error",
    "client_rejected",
    "unknown_client_ignored",
    "client_trust_busy",
    "client_certificate_required",
}


def _credentials(value):
    if (
        not isinstance(value, dict)
        or not all(
            isinstance(value.get(key), str) and _UUID.fullmatch(value[key])
            for key in ("session_id", "instance_id")
        )
        or not isinstance(value.get("resume_token"), str)
        or not _TOKEN.fullmatch(value["resume_token"])
    ):
        raise SloppyError("invalid_input", "Invalid session resume credentials")
    return {key: value[key] for key in ("session_id", "instance_id", "resume_token")}


def connection_path() -> Path:
    if os.environ.get("SLOPPY_CONNECTION_FILE"):
        return Path(os.environ["SLOPPY_CONNECTION_FILE"])
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Sloppy/connection.json"
    if os.name == "nt":
        if not os.environ.get("APPDATA"):
            raise SloppyError("discovery_error", "APPDATA is not set")
        return Path(os.environ["APPDATA"]) / "Sloppy/connection.json"
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        / "sloppy/connection.json"
    )


def discover(file: str | Path | None = None) -> dict:
    file = Path(file or connection_path())
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
    except OSError as error:
        raise discovery_read_error(error, file) from error
    except ValueError as error:
        raise SloppyError(
            "discovery_error",
            "The Sloppy connection file is not valid service discovery JSON. Restart Sloppy to recreate it, or check the configured connection file.",
        ) from error
    if not isinstance(value, dict):
        raise SloppyError(
            "discovery_error",
            "The Sloppy connection file is not a service discovery object. Restart Sloppy to recreate it, or check the configured connection file.",
        )
    try:
        endpoint = canonical_endpoint(str(value.get("base_url")))
    except SloppyError as error:
        raise SloppyError(
            "discovery_error",
            "The Sloppy connection file has an invalid HTTPS service address. Restart Sloppy to recreate it, or check the configured connection file.",
        ) from error
    if (
        value.get("protocol_version") != 1
        or value.get("service") != "SloppyService"
        or not re.fullmatch(r"[0-9a-fA-F]{64}", str(value.get("token")))
    ):
        raise SloppyError(
            "discovery_error",
            "The Sloppy connection file has invalid or unsupported service metadata. Restart Sloppy to recreate it and check that the SDK and Sloppy versions are compatible.",
        )
    try:
        uuid.UUID(value["instance_id"])
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise SloppyError(
            "discovery_error",
            "The Sloppy connection file has an invalid service instance identity. Restart Sloppy to recreate it, or check the configured connection file.",
        ) from error
    return {**value, "base_url": endpoint}


def _item(value) -> dict:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("id"), str)
        or not _UUID.fullmatch(value["id"])
        or not isinstance(value.get("requires_response"), bool)
        or value.get("state") not in {"pending", "submitted", "resolved", "cancelled"}
    ):
        raise SloppyError("protocol_error", "Invalid bubble notification")
    if value.get("resolution") is not None or (
        value["requires_response"] and value["state"] in {"submitted", "resolved"}
    ):
        if not isinstance(value.get("resolution"), dict) or not isinstance(
            value["resolution"].get("answer"), dict
        ):
            raise SloppyError("protocol_error", "Invalid bubble answer")
    if value.get("cancellation") is not None and (
        not isinstance(value["cancellation"], dict)
        or not isinstance(value["cancellation"].get("reason"), str)
    ):
        raise SloppyError("protocol_error", "Invalid bubble cancellation")
    return value


class SessionFuture(Future):
    """Result usable with .result() or await; callbacks never block transport."""

    def __init__(self, dispatch):
        super().__init__()
        self._dispatch = dispatch

    def __await__(self):
        return asyncio.wrap_future(self).__await__()

    def add_done_callback(self, fn):
        super().add_done_callback(lambda future: self._dispatch(fn, future))


class BubbleFuture(SessionFuture):
    """Awaitable response future, with stable .id and a separate .created future."""

    def __init__(self, id: str, dispatch):
        super().__init__(dispatch)
        self.id = id
        self.created = SessionFuture(dispatch)
        self._attempted = False


class SloppySession:
    def __init__(
        self,
        *,
        plugin_id: str | None = None,
        plugin_version: str | None = None,
        application: str | None = None,
        descriptor: dict | None = None,
        connection_file: str | Path | None = None,
        endpoint: str | None = None,
        identity_directory: str | Path | None = None,
        identity_profile: str | None = None,
        confirm_server=None,
        resume: dict | None = None,
        retry_delay: float = 0.5,
        max_retry_delay: float = 30,
    ):
        self.plugin_id = plugin_id or os.environ.get("SLOPPY_PLUGIN_ID")
        if (
            not isinstance(self.plugin_id, str)
            or not self.plugin_id.strip()
            or len(self.plugin_id) > 256
        ):
            raise SloppyError(
                "invalid_input", "Specify plugin_id or launch with sloppy"
            )
        self.application = application or self.plugin_id
        self.descriptor = deepcopy(descriptor)
        self.plugin_version = (
            plugin_version
            or os.environ.get("SLOPPY_PLUGIN_VERSION")
            or (descriptor or {}).get("version")
        )
        self.connection_file = connection_file
        self.endpoint = endpoint or os.environ.get("SLOPPY_SERVICE_URL")
        self.confirm_server = confirm_server
        self.identity = SloppyIdentity(
            application=self.application,
            plugin_id=self.plugin_id,
            directory=identity_directory,
            profile=identity_profile,
        )
        if (
            not all(
                isinstance(value, (int, float)) and math.isfinite(value) and value > 0
                for value in (retry_delay, max_retry_delay)
            )
            or max_retry_delay < retry_delay
        ):
            raise SloppyError(
                "invalid_input", "Retry delays must be positive and bounded"
            )
        self._saved_credentials = _credentials(resume) if resume is not None else None
        self.retry_delay, self.max_retry_delay = retry_delay, max_retry_delay
        self.online = False
        self.last_error = None
        self.connection = None
        self._requests = {}
        self._bubbles = {}
        self._listeners = {}
        self._lock = threading.RLock()
        self._diagnostic_id = str(uuid.uuid4())
        self._diagnostics = SessionLog(
            Path(connection_file or connection_path()).parent
            / "logs"
            / "client-sessions",
            self._diagnostic_id,
        )
        self._attempts = 0
        self._initial_diagnostics = []
        self.diagnostic_error = None
        self._stopped = threading.Event()
        self._socket = None
        self._callbacks = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sloppy-sdk-callback"
        )
        self.ready = SessionFuture(self._dispatch)
        self._thread = threading.Thread(
            target=self._run, name="sloppy-sdk-session", daemon=True
        )
        self._thread.start()

    @property
    def resume_credentials(self):
        return deepcopy(self._saved_credentials)

    @property
    def session_id(self):
        return (
            self.resume_credentials["session_id"] if self.resume_credentials else None
        )

    @property
    def instance_id(self):
        return (
            self.resume_credentials["instance_id"] if self.resume_credentials else None
        )

    @property
    def diagnostic_file(self):
        return self._diagnostics.path(self.session_id or self._diagnostic_id)

    def _diagnostic(self, event, **detail):
        with self._lock:
            detail = {"attempt": self._attempts, **detail}
            if not self.session_id:
                self._initial_diagnostics.append((event, detail))
                self._initial_diagnostics = self._initial_diagnostics[-64:]
            try:
                written = self._diagnostics.record(
                    self.session_id or self._diagnostic_id, event, detail
                )
                if written:
                    self.diagnostic_error = None
            except OSError:
                self.diagnostic_error = SloppyError(
                    "diagnostic_error", "Cannot write private Sloppy session log"
                )
                self._emit("diagnostic_error", self.diagnostic_error)

    def on(self, event, callback):
        with self._lock:
            self._listeners.setdefault(event, []).append(callback)
        return callback

    def _emit(self, event, value):
        with self._lock:
            listeners = list(self._listeners.get(event, []))
            if not self._stopped.is_set():
                for callback in listeners:
                    self._dispatch(callback, value)

    def _dispatch(self, callback, value):
        def invoke():
            try:
                callback(value)
            except Exception:
                _LOG.exception("Sloppy SDK callback failed")

        try:
            self._callbacks.submit(invoke)
        except RuntimeError:
            if not self._stopped.is_set():
                raise
            threading.Thread(
                target=invoke, name="sloppy-sdk-final-callback", daemon=True
            ).start()

    def _status(self, online, error):
        self.online, self.last_error = online, error
        self._emit("status", dict(online=online, error=error))

    @staticmethod
    def _finish(future, *, value=None, error=None):
        try:
            if not future.done():
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(value)
        except InvalidStateError:
            if not future.done():
                raise

    def _fail(self, error):
        self._stopped.set()
        self.online, self.last_error = False, str(error)
        with self._lock:
            self._finish(self.ready, error=error)
            for request in list(self._requests.values()):
                self._finish(request["future"], error=error)
            for bubble in list(self._bubbles.values()):
                self._finish(bubble, error=error)
            self._requests.clear()
            self._bubbles.clear()
        # Fatal status must reach listeners even though further retries are stopped.
        for callback in list(self._listeners.get("status", [])):
            self._dispatch(callback, {"online": False, "error": str(error)})

    def _lose_session(self):
        self._diagnostic("session_lost")
        error = SloppyError(
            "session_lost",
            "Sloppy restarted or no longer retains this session; old bubbles were not replayed",
        )
        with self._lock:
            self._saved_credentials = None
            for id, request in list(self._requests.items()):
                if request["attempted"]:
                    self._finish(request["future"], error=error)
                    del self._requests[id]
            for id, bubble in list(self._bubbles.items()):
                if bubble._attempted:
                    self._finish(bubble, error=error)
                    del self._bubbles[id]
        self._emit("session_lost", error)

    def _run(self):
        delay = self.retry_delay
        while not self._stopped.is_set():
            self._attempts += 1
            self._diagnostic(
                "connect_attempt",
                resumed=bool(self.resume_credentials),
                pending_request_count=len(self._requests),
            )
            try:
                connection = (
                    {"base_url": canonical_endpoint(self.endpoint)}
                    if self.endpoint
                    else discover(self.connection_file)
                )
                self.connection = connection
                if self.descriptor is None and os.environ.get("SLOPPY_PLUGIN_MANIFEST"):
                    manifest = json.loads(
                        Path(os.environ["SLOPPY_PLUGIN_MANIFEST"]).read_text(
                            encoding="utf-8"
                        )
                    )
                    if manifest.get("manifest_version") != 1 or not isinstance(
                        manifest.get("plugin"), dict
                    ):
                        raise SloppyError(
                            "invalid_plugin", "Unsupported plugin manifest"
                        )
                    self.descriptor = manifest["plugin"]
                self.identity._load()
                try:
                    service = self.identity.verify_service(connection["base_url"])
                except SloppyTrustRequiredError as error:
                    if (
                        error.known_fingerprint is not None
                        or self.confirm_server is None
                        or self.confirm_server(error.presented) is not True
                    ):
                        raise
                    if self._stopped.is_set():
                        return
                    self.identity.remember_service(error.presented)
                    service = error.presented
                if (
                    self.resume_credentials
                    and connection.get("instance_id")
                    and self.resume_credentials["instance_id"]
                    != connection["instance_id"]
                ):
                    self._lose_session()
                socket = connect(
                    connection["base_url"].replace("https:", "wss:") + "/v1/sessions",
                    ssl=self.identity._client_context(service.fingerprint),
                    additional_headers=(
                        {"Authorization": "Bearer " + connection["token"]}
                        if "token" in connection
                        else {}
                    ),
                    proxy=None,
                    open_timeout=3,
                    close_timeout=1,
                    ping_interval=None,
                    compression=None,
                    max_size=2 * 1024 * 1024,
                )
                with self._lock:
                    self._socket = socket
                with socket:
                    if self._stopped.is_set():
                        return
                    self._diagnostic(
                        "socket_open", resumed=bool(self.resume_credentials)
                    )
                    trust_ready = False
                    trust_pending = False
                    last_message = time.monotonic()
                    handshake_deadline = last_message + 5
                    heartbeat_timeout = 15.0
                    sent = set()
                    while not self._stopped.is_set():
                        if self.online:
                            with self._lock:
                                requests = list(self._requests.items())
                            for id, request in requests:
                                if id not in sent:
                                    with self._lock:
                                        request["attempted"] = True
                                        bubble = self._bubbles.get(
                                            request["body"].get("id")
                                        )
                                        if bubble:
                                            bubble._attempted = True
                                    encoded = json.dumps(
                                        request["body"], allow_nan=False
                                    )
                                    self._diagnostic(
                                        "sending",
                                        frame_type="request",
                                        bytes=len(encoded.encode("utf-8")),
                                    )
                                    socket.send(encoded)
                                    sent.add(id)
                        try:
                            data = socket.recv(timeout=0.05)
                        except TimeoutError:
                            if (
                                not self.online
                                and time.monotonic() >= handshake_deadline
                            ):
                                raise SloppyError(
                                    "handshake_timeout",
                                    "Sloppy session handshake timed out",
                                )
                            if (
                                self.online
                                and time.monotonic() - last_message >= heartbeat_timeout
                            ):
                                raise SloppyError(
                                    "heartbeat_timeout",
                                    "Sloppy connection heartbeat timed out",
                                )
                            continue
                        body = json.loads(data)
                        if not isinstance(body, dict):
                            raise SloppyError(
                                "protocol_error", "Expected a session message object"
                            )
                        if body.get("type") != "ping":
                            self._diagnostic(
                                "received",
                                frame_type=body["type"]
                                if body.get("type")
                                in {"ready", "reply", "notification"}
                                else "other",
                                bytes=len(data.encode("utf-8"))
                                if isinstance(data, str)
                                else len(data),
                                **(
                                    {"notification_count": len(body["notifications"])}
                                    if isinstance(body.get("notifications"), list)
                                    else {}
                                ),
                            )
                        last_message = time.monotonic()
                        if body.get("type") == "trust_pending":
                            timeout_ms = body.get("timeout_ms")
                            if (
                                trust_ready
                                or trust_pending
                                or self.online
                                or type(timeout_ms) not in {int, float}
                                or not math.isfinite(timeout_ms)
                                or not 0 < timeout_ms <= 120000
                            ):
                                raise SloppyError(
                                    "protocol_error", "Invalid client trust negotiation"
                                )
                            trust_pending = True
                            handshake_deadline = (
                                time.monotonic() + timeout_ms / 1000 + 1
                            )
                            self._status(
                                False,
                                "Waiting for client certificate approval in Sloppy",
                            )
                        elif body.get("type") == "trust_ready":
                            if (
                                trust_ready
                                or self.online
                                or body.get("protocol_version") != 1
                            ):
                                raise SloppyError(
                                    "protocol_error",
                                    "Invalid client trust confirmation",
                                )
                            trust_ready = True
                            self.plugin_version = self.plugin_version or (
                                self.descriptor or {}
                            ).get("version")
                            handshake_deadline = time.monotonic() + 5
                            socket.send(
                                json.dumps(
                                    dict(
                                        type="hello",
                                        protocol_version=1,
                                        plugin_id=self.plugin_id,
                                        plugin_version=self.plugin_version
                                        or (self.descriptor or {}).get("version"),
                                        application=self.application,
                                        descriptor=self.descriptor,
                                        resume=self.resume_credentials,
                                    ),
                                    allow_nan=False,
                                )
                            )
                        elif body.get("type") == "ping":
                            socket.send(json.dumps(dict(type="pong")))
                        elif body.get("type") == "ready":
                            if (
                                body.get("protocol_version") != 1
                                or not isinstance(body.get("instance_id"), str)
                                or not _UUID.fullmatch(body["instance_id"])
                                or (
                                    connection.get("instance_id") is not None
                                    and body.get("instance_id")
                                    != connection["instance_id"]
                                )
                                or not trust_ready
                                or not isinstance(body.get("session_id"), str)
                                or not _UUID.fullmatch(body["session_id"])
                                or not isinstance(body.get("resume_token"), str)
                                or not _TOKEN.fullmatch(body["resume_token"])
                                or self.online
                                or not isinstance(
                                    body.get("heartbeat_timeout_ms"), (int, float)
                                )
                                or not math.isfinite(body["heartbeat_timeout_ms"])
                                or not 50 <= body["heartbeat_timeout_ms"] <= 120000
                                or not isinstance(
                                    body.get("heartbeat_interval_ms"), (int, float)
                                )
                                or not 0
                                < body["heartbeat_interval_ms"]
                                < body["heartbeat_timeout_ms"]
                                or not isinstance(body.get("notifications"), list)
                            ):
                                raise SloppyError(
                                    "protocol_error", "Invalid session handshake"
                                )
                            snapshot = [_item(item) for item in body["notifications"]]
                            if any(
                                item.get("session_id") != body["session_id"]
                                for item in snapshot
                            ):
                                raise SloppyError(
                                    "protocol_error",
                                    "Notification belongs to another session",
                                )
                            reply_ids = body.get("pending_reply_ids", [])
                            if not isinstance(reply_ids, list) or any(
                                not isinstance(id, str) or not _UUID.fullmatch(id)
                                for id in reply_ids
                            ):
                                raise SloppyError(
                                    "protocol_error", "Invalid pending reply snapshot"
                                )
                            self.plugin_version = body.get(
                                "plugin_version", self.plugin_version
                            )
                            self._saved_credentials = {
                                key: body[key]
                                for key in ("session_id", "resume_token", "instance_id")
                            }
                            for event, detail in self._initial_diagnostics:
                                self._diagnostic(event, **detail)
                            self._initial_diagnostics = []
                            self._diagnostic(
                                "ready",
                                resumed=body.get("resumed") is True,
                                notification_count=len(snapshot),
                                pending_reply_count=len(reply_ids),
                            )
                            heartbeat_timeout = (
                                body.get("heartbeat_timeout_ms", 15000) / 1000
                            )
                            delay = self.retry_delay
                            self._status(True, None)
                            for item in snapshot:
                                self._update(item)
                            self._finish(self.ready)
                            self._emit("ready", deepcopy(self.resume_credentials))
                            with self._lock:
                                received = [
                                    id for id in reply_ids if id not in self._requests
                                ]
                            for id in received:
                                socket.send(
                                    json.dumps({"type": "received", "request_id": id})
                                )
                        elif body.get("type") == "error":
                            if body.get("code") == "session_unknown":
                                self._lose_session()
                            raise SloppyError(
                                body.get("code", "protocol_error"),
                                service_message(
                                    body.get("code"),
                                    body.get("message", "Session failed"),
                                    self.plugin_id
                                    + (
                                        "@" + self.plugin_version
                                        if self.plugin_version
                                        else ""
                                    ),
                                ),
                            )
                        elif body.get("type") == "notification":
                            if not self.online:
                                raise SloppyError(
                                    "protocol_error",
                                    "Expected a session handshake first",
                                )
                            self._update(_item(body.get("notification")))
                        elif body.get("type") == "reply":
                            if not self.online:
                                raise SloppyError(
                                    "protocol_error",
                                    "Expected a session handshake first",
                                )
                            id = body.get("request_id")
                            if not isinstance(id, str) or not isinstance(
                                body.get("ok"), bool
                            ):
                                raise SloppyError(
                                    "protocol_error", "Invalid session reply"
                                )
                            with self._lock:
                                request = self._requests.get(id)
                            if request:
                                if body["ok"]:
                                    value = body.get("value")
                                    if request["body"]["op"] == "list":
                                        if not isinstance(value, list):
                                            raise SloppyError(
                                                "protocol_error",
                                                "Expected a bubble list",
                                            )
                                        value = [_item(item) for item in value]
                                        items = value
                                    else:
                                        value = _item(value)
                                        items = [value]
                                    if any(
                                        item.get("session_id") != self.session_id
                                        for item in items
                                    ):
                                        raise SloppyError(
                                            "protocol_error",
                                            "Notification belongs to another session",
                                        )
                                    self._finish(
                                        request["future"], value=deepcopy(value)
                                    )
                                else:
                                    self._finish(
                                        request["future"],
                                        error=SloppyError(
                                            "request_failed",
                                            body.get("error", "Request failed"),
                                            body.get("status"),
                                        ),
                                    )
                                with self._lock:
                                    self._requests.pop(id, None)
                            socket.send(
                                json.dumps(dict(type="received", request_id=id))
                            )
                        else:
                            raise SloppyError(
                                "protocol_error", "Unknown session message type"
                            )
            except (
                OSError,
                WebSocketException,
                SloppyError,
                ValueError,
                TypeError,
            ) as error:
                if not self._stopped.is_set():
                    if isinstance(error, SloppyError) and error.code in {
                        "service_unavailable",
                        "discovery_error",
                    }:
                        self._diagnostic(
                            "disconnected"
                            if error.code == "service_unavailable"
                            else "discovery_error",
                            code=error.code,
                        )
                    elif isinstance(error, SloppyError):
                        code = (
                            error.code
                            if error.code
                            in {
                                "handshake_timeout",
                                "heartbeat_timeout",
                                "protocol_error",
                                "session_denied",
                                "invalid_handshake",
                                "invalid_plugin",
                                "session_unknown",
                                "plugin_disabled",
                                "service_stopped",
                            }
                            else "other"
                        )
                        self._diagnostic(
                            code
                            if code in {"handshake_timeout", "heartbeat_timeout"}
                            else "protocol_error",
                            code=code,
                        )
                    elif isinstance(error, OSError):
                        self._diagnostic("transport_error", os_code=error.errno)
                    elif isinstance(error, WebSocketException):
                        closed = getattr(error, "sent", None) or getattr(
                            error, "rcvd", None
                        )
                        if closed is None or closed.code not in {1000, 1001}:
                            self._diagnostic("transport_error", code="websocket")
                        if closed is not None:
                            self._diagnostic("socket_closed", close_code=closed.code)
                    else:
                        self._diagnostic("protocol_error")
                    self._diagnostic("disconnected")
                    if isinstance(error, SloppyError) and error.code in _FATAL:
                        self._fail(error)
                    elif isinstance(error, (ValueError, TypeError)):
                        self._fail(SloppyError("protocol_error", str(error)))
                    else:
                        self._status(False, str(connection_error(error)))
            finally:
                with self._lock:
                    self._socket = None
                self.online = False
            if not self._stopped.is_set():
                self._diagnostic("retry_scheduled", delay_ms=int(delay * 1000))
            if self._stopped.wait(delay):
                break
            delay = min(delay * 2, self.max_retry_delay)

    def _update(self, item, emit=True):
        if item.get("session_id") != self.session_id:
            raise SloppyError(
                "protocol_error", "Notification belongs to another session"
            )
        with self._lock:
            bubble = self._bubbles.get(item["id"])
            if bubble:
                if item["state"] in {"submitted", "resolved"} and item.get(
                    "resolution"
                ):
                    self._finish(bubble, value=deepcopy(item["resolution"]["answer"]))
                    self._bubbles.pop(item["id"], None)
                elif item["state"] == "cancelled":
                    self._finish(
                        bubble,
                        error=SloppyError(
                            "dismissed",
                            (item.get("cancellation") or {}).get(
                                "reason", "Bubble dismissed without an answer"
                            ),
                        ),
                    )
                if item["state"] in {"resolved", "cancelled"}:
                    self._bubbles.pop(item["id"], None)
        if emit:
            self._emit("notification", deepcopy(item))

    def _request(self, op, **body):
        future = SessionFuture(self._dispatch)
        with self._lock:
            if self._stopped.is_set():
                self._finish(
                    future, error=SloppyError("closed", "Sloppy session is closed")
                )
                return future
            if len(self._requests) >= 128:
                self._finish(
                    future,
                    error=SloppyError(
                        "queue_full", "Too many pending session operations"
                    ),
                )
                return future
            id = str(uuid.uuid4())
            message = dict(body, type="request", request_id=id, op=op)
            try:
                encoded = json.dumps(message, allow_nan=False)
                if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
                    raise ValueError("Session requests must not exceed 2 MiB")
            except (ValueError, TypeError) as error:
                self._finish(future, error=SloppyError("invalid_input", str(error)))
                return future
            self._requests[id] = dict(
                body=deepcopy(message), future=future, attempted=False
            )
        return future

    def send(self, content: dict) -> BubbleFuture:
        id = str(uuid.uuid4())
        bubble = BubbleFuture(id, self._dispatch)
        if not isinstance(content, dict):
            error = SloppyError("invalid_input", "A notification must be a JSON object")
            self._finish(bubble.created, error=error)
            self._finish(bubble, error=error)
            return bubble
        with self._lock:
            self._bubbles[id] = bubble
        payload = deepcopy(content)
        payload.setdefault(
            "origin",
            dict(application=self.application, plugin=self.plugin_id, instance="sdk"),
        )
        created = self._request("create", id=id, notification=payload)

        def finished(future):
            try:
                item = _item(future.result())
                self._finish(bubble.created, value=deepcopy(item))
                if not item["requires_response"]:
                    self._finish(bubble, value=None)
                    with self._lock:
                        self._bubbles.pop(id, None)
                self._update(item, emit=False)
            except Exception as error:
                self._finish(bubble.created, error=error)
                self._finish(bubble, error=error)
                with self._lock:
                    self._bubbles.pop(id, None)

        Future.add_done_callback(created, finished)
        return bubble

    def list(self) -> SessionFuture:
        return self._request("list")

    def get(self, id: str) -> SessionFuture:
        return self._request("get", id=id)

    def answer(self, id: str, answer: dict) -> SessionFuture:
        return self._request(
            "answer", id=id, answer=dict(answer, source=self.application)
        )

    def resolve(self, id: str, answer: dict) -> SessionFuture:
        return self._request(
            "resolve", id=id, answer=dict(answer, source=self.application)
        )

    def acknowledge(
        self, id: str, status: str, message: str | None = None
    ) -> SessionFuture:
        return self._request(
            "acknowledge",
            id=id,
            acknowledgement=dict(source="sdk", status=status, message=message),
        )

    def dismiss(
        self, id: str, reason: str = "Dismissed by originating application"
    ) -> SessionFuture:
        return self._request("dismiss", id=id, reason=reason)

    def regenerate_certificate(self) -> str:
        self.close()
        return self.identity.regenerate()

    def close(self):
        if not self._stopped.is_set():
            self._diagnostic("closed")
        self._stopped.set()
        with self._lock:
            socket = self._socket
        if socket:
            socket.close(reason="Plugin session closed")
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)
        error = SloppyError("closed", "Sloppy session closed")
        with self._lock:
            self._finish(self.ready, error=error)
            for request in list(self._requests.values()):
                self._finish(request["future"], error=error)
            for bubble in list(self._bubbles.values()):
                self._finish(bubble, error=error)
            self._requests.clear()
            self._bubbles.clear()
        self._callbacks.shutdown(wait=False)
        self.online = False

    def __enter__(self):
        self.ready.result()
        return self

    def __exit__(self, *_):
        self.close()


from .actions import ActionContext, SloppyActionsProvider  # noqa: E402
