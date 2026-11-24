"""Private metadata-only diagnostics shared by all Python plugins."""

import json
import os
import stat
import time
from pathlib import Path

MAX_BYTES = 1024 * 1024
_RANK = {"error": 0, "info": 1, "debug": 2}


class SessionLog:
    def __init__(self, directory: Path, client_id: str):
        self.directory, self.client_id = directory, client_id

    def path(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.log"

    def verbosity(self):
        try:
            descriptor = os.open(
                self.directory.parent / "verbosity.json",
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            return "info"
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
                raise OSError("Invalid private log verbosity file")
            try:
                value = json.loads(os.read(descriptor, 4097))
            except ValueError as error:
                raise OSError("Invalid log verbosity") from error
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("level"), str)
                or value["level"] not in _RANK
            ):
                raise OSError("Invalid log verbosity")
            return value["level"]
        finally:
            os.close(descriptor)

    def record(self, session_id: str, event: str, detail: dict):
        for directory in (self.directory.parent, self.directory):
            directory.mkdir(mode=0o700, exist_ok=True)
            metadata = directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise OSError("Session log directory must be a regular directory")
            if os.name != "nt":
                directory.chmod(0o700)
        level = (
            "debug"
            if event in {"sending", "received"}
            else (
                "error"
                if event
                in {
                    "transport_error",
                    "protocol_error",
                    "handshake_timeout",
                    "heartbeat_timeout",
                }
                else "info"
            )
        )
        if event == "socket_closed" and detail.get("close_code") not in {1000, 1001}:
            level = "error"
        if _RANK[level] > _RANK[self.verbosity()]:
            return False
        line = (
            json.dumps(
                {
                    "timestamp_ms": int(time.time() * 1000),
                    "source": "sdk",
                    "client_id": self.client_id,
                    "session_id": session_id,
                    "event": event,
                    "level": level,
                    "detail": detail,
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        path = self.path(session_id)
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("Session log must be a regular private file")
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            if metadata.st_size + len(line) > MAX_BYTES:
                os.close(descriptor)
                descriptor = -1
                previous = Path(str(path) + ".1")
                try:
                    if not stat.S_ISREG(previous.lstat().st_mode):
                        raise OSError("Rotated log must be a regular file")
                except FileNotFoundError:
                    pass
                path.replace(previous)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            while line:
                written = os.write(descriptor, line)
                if written == 0:
                    raise OSError("Session log write made no progress")
                line = line[written:]
        finally:
            if descriptor != -1:
                os.close(descriptor)
        return True
