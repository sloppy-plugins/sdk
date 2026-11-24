"""Actions protocol v1: independent providers, explicit invocation, no replay."""

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy

from websockets.sync.client import connect

from . import SloppyError, discover
from .security import SloppyIdentity, SloppyTrustRequiredError, canonical_endpoint

_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())


class ActionContext:
    """One admitted execution. Handlers must cooperate with cancellation."""

    def __init__(self, frame):
        self.invocation_id = frame["invocation_id"]
        self.provider_id = frame["provider_id"]
        self.deadline = time.monotonic() + frame["timeout_ms"] / 1000
        self.cancelled = threading.Event()

    def check_cancelled(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise SloppyError(
                "cancelled", "The action was cancelled or its deadline passed"
            )


class SloppyActionsProvider:
    """Publish parameterless actions; collect runtime input using the bubble API.

    Register the static catalog before ``start``. The authenticated identity and
    provider key determine a persistent slot, not the display name. A process
    keeps its execution epoch and duplicate high-water mark across reconnects.
    """

    def __init__(
        self,
        *,
        plugin_id,
        name,
        descriptor,
        categories=None,
        provider_key="default",
        connection_file=None,
        endpoint=None,
        identity_directory=None,
        identity_profile=None,
        application=None,
        confirm_server=None,
    ):
        if not isinstance(provider_key, str) or not 1 <= len(provider_key) <= 64:
            raise SloppyError(
                "invalid_input", "provider_key must contain 1–64 characters"
            )
        if descriptor.get("id") != plugin_id:
            raise SloppyError(
                "invalid_input", "The descriptor must identify this plugin"
            )
        self.plugin_id, self.name, self.provider_key = plugin_id, name, provider_key
        self.descriptor = deepcopy(descriptor)
        self.categories = deepcopy(categories or [])
        if len(self.categories) > 32 or len(
            {category.get("key") for category in self.categories}
        ) != len(self.categories):
            raise SloppyError("invalid_input", "Action categories require unique keys")
        self.connection_file, self.endpoint = (
            connection_file,
            endpoint or os.environ.get("SLOPPY_SERVICE_URL"),
        )
        self.confirm_server = confirm_server
        self.identity = SloppyIdentity(
            application=application or plugin_id,
            plugin_id=plugin_id,
            directory=identity_directory,
            profile=identity_profile,
        )
        self.execution_epoch = str(uuid.uuid4())
        self.catalog_revision = str(uuid.uuid4())
        self.provider_id = None
        self.online = False
        self.last_error = None
        self.ready = Future()
        self._definitions, self._handlers = {}, {}
        self._high_water = 0
        self._results = OrderedDict()
        self._fingerprints = {}
        self._unacknowledged = set()
        self._active = {}
        self._token = None
        self._socket = None
        self._thread = None
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="sloppy-action"
        )

    def register_action(self, definition, run):
        if self._thread is not None:
            raise SloppyError(
                "invalid_input", "Register the catalog before starting the provider"
            )
        definition = deepcopy(definition)
        definition.setdefault("input_schema", deepcopy(_EMPTY))
        key = definition.get("key")
        timeout = definition.get("timeout_ms", 60_000)
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 128
            or key in self._handlers
            or not callable(run)
            or not isinstance(definition.get("title"), str)
            or not definition["title"].strip()
            or len(definition["title"].encode()) > 240
            or any(ord(c) < 32 or ord(c) == 127 for c in definition["title"])
            or not isinstance(definition.get("description", ""), str)
            or len(definition.get("description", "").encode()) > 4096
            or set(definition)
            - {
                "key",
                "title",
                "description",
                "input_schema",
                "timeout_ms",
                "interaction",
                "prompt",
                "category",
            }
            or definition["input_schema"] != _EMPTY
            or type(timeout) is not int
            or not 1000 <= timeout <= 1_800_000
            or len(self._definitions) >= 256
        ):
            raise SloppyError(
                "invalid_input", "Invalid action definition or duplicate key"
            )
        if definition.get("category") is not None and definition["category"] not in {
            category["key"] for category in self.categories
        }:
            raise SloppyError(
                "invalid_input", "Action refers to an undeclared category"
            )
        interaction = definition.get("interaction", "immediate")
        prompt = definition.get("prompt")
        if (
            (interaction == "immediate" and prompt is not None)
            or (
                interaction == "input_bubble"
                and (
                    not isinstance(prompt, dict)
                    or prompt.get("type") != "questionnaire"
                )
            )
            or interaction not in {"immediate", "input_bubble"}
        ):
            raise SloppyError(
                "invalid_input", "Declare the questionnaire for input-bubble actions"
            )
        self._definitions[key], self._handlers[key] = definition, run
        if (
            len(
                json.dumps(
                    list(self._definitions.values()), ensure_ascii=False
                ).encode()
            )
            > 240 * 1024
        ):
            del self._definitions[key], self._handlers[key]
            raise SloppyError("invalid_input", "Actions catalog exceeds 240 KiB")

    def start(self):
        if self._thread is not None:
            raise SloppyError("invalid_input", "This provider has already started")
        self._thread = threading.Thread(
            target=self._run, name="sloppy-actions", daemon=True
        )
        self._thread.start()
        return self

    def _send(self, body):
        encoded = json.dumps(body, allow_nan=False, ensure_ascii=False)
        if len(encoded.encode()) > 256 * 1024:
            raise SloppyError("invalid_input", "Actions frames must not exceed 256 KiB")
        with self._lock:
            if self._socket is not None:
                self._socket.send(encoded)

    def _result(self, id, result):
        try:
            if len(json.dumps(result, allow_nan=False).encode()) > 65_536:
                raise ValueError("Result exceeds budget")
        except (ValueError, TypeError):
            result = {
                "status": "failed",
                "error": {
                    "code": "invalid_result",
                    "message": "The action returned an invalid or oversized result.",
                },
            }
        with self._lock:
            self._active.pop(id, None)
            self._results[id] = deepcopy(result)
            self._unacknowledged.add(id)
            while len(self._results) > 256:
                retired, _ = self._results.popitem(last=False)
                self._unacknowledged.discard(retired)
                self._fingerprints.pop(retired, None)
            try:
                if self.online:
                    self._send(
                        {"type": "invoke_result", "invocation_id": id, "result": result}
                    )
            except Exception as error:
                # Retained until receipt; a reconnect resends results, never work.
                self.last_error = str(error)

    def _execute(self, frame, context):
        try:
            context.check_cancelled()
            result = self._handlers[frame["action_key"]](
                deepcopy(frame["input"]), context
            )
            if not isinstance(result, dict) or result.get("status") not in (
                "succeeded",
                "failed",
                "cancelled",
                "outcome_unknown",
            ):
                raise ValueError("Action handlers must return an ActionResult")
        except SloppyError as error:
            result = (
                {"status": "cancelled"}
                if error.code == "cancelled"
                else {
                    "status": "failed",
                    "error": {"code": error.code, "message": str(error)},
                }
            )
        except Exception:
            _LOG.exception("Action handler failed")
            result = {
                "status": "failed",
                "error": {
                    "code": "handler_error",
                    "message": "The plugin action failed. See its local diagnostics.",
                },
            }
        self._result(context.invocation_id, result)

    def _invoke(self, frame):
        id = frame.get("invocation_id")
        try:
            uuid.UUID(id)
        except (ValueError, TypeError, AttributeError) as error:
            raise SloppyError("protocol_error", "Invalid invocation ID") from error
        with self._lock:
            if (
                frame.get("execution_epoch") != self.execution_epoch
                or frame.get("provider_id") != self.provider_id
                or frame.get("catalog_revision") != self.catalog_revision
            ):
                raise SloppyError(
                    "protocol_error",
                    "Invocation is addressed to another provider generation",
                )
            sequence = frame.get("invoke_sequence")
            if type(sequence) is not int or sequence <= 0:
                raise SloppyError("protocol_error", "Invalid invocation sequence")
            fingerprint = hashlib.sha256(
                json.dumps(frame, sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
            if id in self._fingerprints and self._fingerprints[id] != fingerprint:
                raise SloppyError(
                    "protocol_error", "An invocation's immutable payload changed"
                )
            if id in self._results:
                self._send(
                    {
                        "type": "invoke_result",
                        "invocation_id": id,
                        "result": self._results[id],
                    }
                )
                return
            if id in self._active:
                self._send({"type": "invoke_accepted", "invocation_id": id})
                return
            if sequence <= self._high_water:
                self._result(
                    id,
                    {
                        "status": "outcome_unknown",
                        "message": "This invocation has already been admitted; its retained result is no longer available.",
                    },
                )
                return
            self._fingerprints[id] = fingerprint
            self._high_water = sequence  # Advance before admitting the handler.
            if (
                frame.get("input") != {}
                or frame.get("action_key") not in self._handlers
            ):
                self._result(
                    id,
                    {
                        "status": "failed",
                        "error": {
                            "code": "invalid_input",
                            "message": "The action key or input is invalid.",
                        },
                    },
                )
                return
            timeout = frame.get("timeout_ms")
            if type(timeout) is not int or not 0 < timeout <= 1_800_000:
                raise SloppyError("protocol_error", "Invalid invocation deadline")
            if len(self._active) >= 4:
                self._result(
                    id,
                    {
                        "status": "failed",
                        "error": {
                            "code": "busy",
                            "message": "The plugin is already running four actions.",
                        },
                    },
                )
                return
            context = ActionContext(frame)
            self._active[id] = context
            self._send({"type": "invoke_accepted", "invocation_id": id})
            self._executor.submit(self._execute, frame, context)

    def _run(self):
        while not self._stopped.is_set():
            try:
                endpoint = (
                    canonical_endpoint(self.endpoint)
                    if self.endpoint
                    else discover(self.connection_file)["base_url"]
                )
                self.identity._load()
                try:
                    service = self.identity.verify_service(endpoint)
                except SloppyTrustRequiredError as error:
                    if (
                        error.known_fingerprint is not None
                        or self.confirm_server is None
                        or self.confirm_server(error.presented) is not True
                    ):
                        raise
                    self.identity.remember_service(error.presented)
                    service = error.presented
                with connect(
                    endpoint.replace("https:", "wss:") + "/v1/actions",
                    ssl=self.identity._client_context(service.fingerprint),
                    proxy=None,
                    open_timeout=3,
                    close_timeout=1,
                    ping_interval=None,
                    compression=None,
                    max_size=256 * 1024,
                ) as socket:
                    with self._lock:
                        self._socket = socket
                    deadline = time.monotonic() + 5
                    last = time.monotonic()
                    while not self._stopped.is_set():
                        try:
                            body = json.loads(socket.recv(timeout=0.1))
                        except TimeoutError:
                            if time.monotonic() > (
                                last + 15 if self.online else deadline
                            ):
                                raise SloppyError(
                                    "service_unavailable",
                                    "Action provider connection timed out",
                                )
                            continue
                        if not isinstance(body, dict):
                            raise SloppyError(
                                "protocol_error", "Expected an Actions message"
                            )
                        last = time.monotonic()
                        kind = body.get("type")
                        if kind == "trust_pending":
                            deadline = time.monotonic() + min(
                                300, body.get("timeout_ms", 120_000) / 1000
                            )
                        elif kind == "trust_ready":
                            deadline = time.monotonic() + 5
                            self._send(
                                {
                                    "type": "hello",
                                    "actions_version": 1,
                                    "plugin_id": self.plugin_id,
                                    "provider_key": self.provider_key,
                                    "name": self.name,
                                    "descriptor": self.descriptor,
                                    "execution_epoch": self.execution_epoch,
                                    "catalog_revision": self.catalog_revision,
                                    "actions": list(self._definitions.values()),
                                    "categories": self.categories,
                                    "resume_token": self._token,
                                }
                            )
                        elif kind == "ready":
                            if (
                                body.get("actions_version") != 1
                                or body.get("execution_epoch") != self.execution_epoch
                                or body.get("catalog_revision") != self.catalog_revision
                            ):
                                raise SloppyError(
                                    "protocol_error", "Unexpected provider ownership"
                                )
                            uuid.UUID(body["provider_id"])
                            self.provider_id, self._token = (
                                body["provider_id"],
                                body["resume_token"],
                            )
                            self.online, self.last_error = True, None
                            if not self.ready.done():
                                self.ready.set_result(self.provider_id)
                            with self._lock:
                                for id in list(self._unacknowledged):
                                    self._send(
                                        {
                                            "type": "invoke_result",
                                            "invocation_id": id,
                                            "result": self._results[id],
                                        }
                                    )
                        elif kind == "ping":
                            self._send({"type": "pong"})
                        elif kind == "invoke" and self.online:
                            self._invoke(body)
                        elif kind == "cancel" and self.online:
                            with self._lock:
                                context = self._active.get(body.get("invocation_id"))
                                if context:
                                    context.cancelled.set()
                        elif kind == "result_received" and self.online:
                            with self._lock:
                                self._unacknowledged.discard(body.get("invocation_id"))
                        elif kind == "error":
                            raise SloppyError(
                                body.get("code", "protocol_error"),
                                body.get("message", "Actions connection failed"),
                            )
                        else:
                            raise SloppyError(
                                "protocol_error", "Unexpected Actions frame"
                            )
            except Exception as error:
                self.last_error = str(error)
                if isinstance(error, SloppyTrustRequiredError) or (
                    isinstance(error, SloppyError)
                    and error.code
                    in {
                        "invalid_provider",
                        "plugin_disabled",
                        "plugin_version_mismatch",
                        "protocol_error",
                        "client_rejected",
                        "client_certificate_required",
                    }
                ):
                    if not self.ready.done():
                        self.ready.set_exception(error)
                    break
            finally:
                with self._lock:
                    self.online, self._socket = False, None
            self._stopped.wait(0.5)

    def close(self):
        self._stopped.set()
        with self._lock:
            for context in self._active.values():
                context.cancelled.set()
            socket = self._socket
        if socket is not None:
            socket.close()
        if self._thread is not None:
            self._thread.join(timeout=4)
        self._executor.shutdown(wait=False, cancel_futures=True)
        if not self.ready.done():
            self.ready.set_exception(SloppyError("closed", "Actions provider closed"))
