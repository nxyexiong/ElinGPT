"""Authenticated, bounded loopback transport. No automatic mutation retries."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, BinaryIO

MAX_REQUEST = 65536
MAX_RESPONSE = 8 * 1024 * 1024


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def default_descriptor() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise BridgeError("configuration", "LOCALAPPDATA is unset; pass --connection.")
    return Path(root) / "ElinGPT" / "connection.json"


class BridgeClient:
    def __init__(self, descriptor: Path | None = None, *, timeout: float = 15):
        self.descriptor = descriptor or default_descriptor()
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._reader: BinaryIO | None = None
        self._token = ""
        self._session = ""
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None
        self.has_control = False
        self.failure: BridgeError | None = None

    def _connect(self) -> None:
        try:
            if self.descriptor.stat().st_size > 4096:
                raise BridgeError("configuration", "Connection descriptor is too large.")
            config = json.loads(self.descriptor.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise BridgeError("not_connected", "Enable ElinGPT with F8 in a loaded game; "
                              f"cannot read {self.descriptor}: {error}") from error
        if not isinstance(config, dict) or config.get("host") != "127.0.0.1":
            raise BridgeError("configuration", "Only the mod's IPv4 loopback endpoint is allowed.")
        port = config.get("port")
        token = config.get("token")
        session = config.get("session_id")
        if (type(port) is not int or not 1 <= port <= 65535 or
                not isinstance(token, str) or len(token) != 44 or
                not isinstance(session, str) or len(session) != 32):
            raise BridgeError("configuration", "Invalid connection descriptor.")
        try:
            self._socket = socket.create_connection(("127.0.0.1", port), self.timeout)
            self._reader = self._socket.makefile("rb")
        except OSError as error:
            self._drop()
            raise BridgeError("not_connected", "The local mod endpoint is not responsive. "
                              "Check F8 and the BepInEx log.") from error
        self._token = token
        self._session = session

    def _drop(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self.has_control = False

    def call(self, method: str, params: dict[str, Any] | None = None, *,
             request_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self.failure:
                if method not in {"observe", "ping"} or self.failure.code not in {"outcome_unknown", "game_closed"}:
                    raise self.failure
                # Recovery starts with an explicit read, never a replayed mutation or claim.
                self._drop()
                self._connect()
                self.failure = None
                self._stop.clear()
            if self._socket is None:
                self._connect()
            identifier = request_id or uuid.uuid4().hex
            payload = json.dumps({"id": identifier, "token": self._token, "method": method,
                                  "params": params or {}}, separators=(",", ":"), allow_nan=False).encode() + b"\n"
            if len(payload) > MAX_REQUEST:
                raise BridgeError("request_too_large", "Request exceeds 64 KiB.")
            try:
                assert self._socket is not None and self._reader is not None
                self._socket.sendall(payload)
                line = self._reader.readline(MAX_RESPONSE + 1)
                if not line or not line.endswith(b"\n") or len(line) > MAX_RESPONSE:
                    raise ValueError("Incomplete/oversized response")
                response = json.loads(line)
                if not isinstance(response, dict):
                    raise ValueError("Expected a JSON response object")
                if response.get("id") != identifier:
                    error_value = response.get("error")
                    if isinstance(error_value, dict) and error_value.get("code") == "unauthorized":
                        raise BridgeError("unauthorized", "The mod pairing token changed; restart this connector.")
                    raise ValueError("Response ID mismatch")
            except BridgeError as error:
                self._drop()
                self.failure = error
                raise
            except (OSError, ValueError, UnicodeError) as error:
                self._drop()
                self.failure = BridgeError("outcome_unknown", "Local connection failed. A submitted action "
                                           "may have executed; do not retry blindly. Explicitly observe "
                                           "to reconnect and inspect the game before continuing.")
                raise self.failure from error
            if "error" in response:
                value = response["error"]
                if not isinstance(value, dict):
                    raise BridgeError("protocol_error", "Invalid error response.")
                if value.get("code") in {"not_controller", "internal_error"}:
                    self.has_control = False
                raise BridgeError(str(value.get("code", "error")), str(value.get("message", "Bridge error")))
            result = response.get("result")
            if not isinstance(result, dict):
                raise BridgeError("protocol_error", "Expected an object result.")
            if method == "ping" and result.get("session_id") != self._session:
                raise BridgeError("session_changed", "Descriptor and server sessions disagree.")
            if method == "claim":
                self.has_control = True
            elif method == "release":
                self.has_control = False
            elif method == "heartbeat" and self.has_control and not result.get("owns_control"):
                self.has_control = False
                print("ElinGPT: control was released or taken over by the user.", file=sys.stderr)
            if result.get("quitting") is True:
                self._stop.set()
                self._drop()
                self.failure = BridgeError("game_closed", "The game accepted quit; observe after relaunching Elin to reconnect.")
            return result

    def start_heartbeat(self) -> None:
        if self._heartbeat is not None and self._heartbeat.is_alive():
            return

        def keep_alive() -> None:
            while not self._stop.wait(5):
                if self._socket is None:
                    continue
                try:
                    self.call("heartbeat")
                except BridgeError as error:
                    if error.code == "queue_full":
                        print(f"ElinGPT heartbeat delayed: {error}", file=sys.stderr)
                        continue
                    self.failure = error
                    print(f"ElinGPT heartbeat stopped: {error}", file=sys.stderr)
                    return

        self._heartbeat = threading.Thread(target=keep_alive, name="ElinGPT heartbeat", daemon=True)
        self._heartbeat.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            if self.has_control and self.failure is None:
                try:
                    self.call("release")
                except BridgeError as error:
                    print(f"ElinGPT release failed: {error}", file=sys.stderr)
            self._drop()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=self.timeout + 1)

    def __enter__(self) -> BridgeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
