"""Synchronous API client and CLI, with response/push demultiplexing."""

import json
import socket
import sys
from collections import deque
from pathlib import Path

from .api import APIError, socket_path


class Client:
    def __init__(self, root: Path, *, hello=True, notifications=False, timeout=5):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(str(socket_path(root)))
        except Exception:
            self.sock.close()
            raise
        self.file = self.sock.makefile("rwb")
        self.events = deque()
        self.counter = 0
        if hello:
            try:
                self.hello = self.call(
                    "hello",
                    dict(
                        api_version=0,
                        client="troupe-python/0.1",
                        notifications=notifications,
                    ),
                )
            except Exception:
                self.close()
                raise

    def close(self):
        self.file.close()
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def receive(self):
        line = self.file.readline()
        if not line:
            raise ConnectionError("Engine offline")
        return json.loads(line)

    def call(self, method, params=None):
        self.counter += 1
        self.file.write(
            (
                json.dumps(dict(id=self.counter, method=method, params=params or {}))
                + "\n"
            ).encode()
        )
        self.file.flush()
        while True:
            response = self.receive()
            if "event" in response:
                self.events.append(response)
                continue
            if response["id"] != self.counter:
                raise ConnectionError("unexpected response id")
            if not response["ok"]:
                e = response["error"]
                raise APIError(e["code"], e["message"], e.get("data"))
            return response["result"]

    def event(self):
        return self.events.popleft() if self.events else self.receive()


def cli(root, method, raw="{}"):
    try:
        params = json.loads(raw)
        with Client(root) as client:
            result = client.call(method, params)
            if method == "subscribe":
                client.sock.settimeout(None)
                while True:
                    print(json.dumps(client.event()), flush=True)
            else:
                print(json.dumps(result, indent=2))
        return 0
    except (FileNotFoundError, ConnectionRefusedError):
        print("engine not running", file=sys.stderr)
        return 2
    except APIError as e:
        print(f"{e.code}: {e}", file=sys.stderr)
        return 1
    except (ValueError, OSError, ConnectionError) as e:
        print(f"bad_request: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
