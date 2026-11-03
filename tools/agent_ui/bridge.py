"""Opt-in, process-local agent bridge. Bootstrap once inside Binary Ninja."""

from __future__ import annotations

import hmac
import json
import os
import runpy
import secrets
import socketserver
import sys
import threading
from pathlib import Path
from types import ModuleType

HOLDER = '_bnnotes_agent_bridge'
HERE = Path(__file__).resolve().parent


class Handler(socketserver.StreamRequestHandler):
    def send(self, value):
        self.wfile.write(json.dumps(value).encode() + b'\n')
        self.wfile.flush()

    def handle(self):
        self.connection.settimeout(10)
        try:
            line = self.rfile.readline(65537)
            if len(line) > 65536:
                raise ValueError('Request is too large.')
            message = json.loads(line)
            if not hmac.compare_digest(str(message.get('token', '')), self.server.token):
                self.send({'error': 'Unauthorized'})
                return
            operation = message.get('operation')
            if operation == 'ping':
                self.send(self.server.info())
            elif operation == 'stop':
                self.send({'stopping': True})
                self.server.stop()
            elif operation == 'run':
                path = Path(message['request']).resolve(strict=True)
                request = json.loads(path.read_text())
                if request['pid'] != os.getpid():
                    raise ValueError('Request targets a different process.')
                if not self.server.job_lock.acquire(blocking=False):
                    self.send({'error': 'Another agent UI script is still running.'})
                    return
                try:
                    self.send({'accepted': request['run_id'], 'pid': os.getpid()})
                    # Every invocation sees current inspection code. This does
                    # not reload the installed plugin's live Qt classes.
                    runpy.run_path(str(HERE / 'runtime.py'))['execute'](str(path))
                finally:
                    self.server.job_lock.release()
            else:
                raise ValueError(f'Unknown bridge operation: {operation}')
        except (OSError, KeyError, TypeError, ValueError) as error:
            try:
                self.send({'error': str(error)})
            except OSError:
                pass


class Bridge(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.job_lock = threading.Lock()
        self.stopped = False
        self.descriptor = HERE.parents[1] / '.agent-artifacts/bridges' / f'{os.getpid()}.json'
        super().__init__(('127.0.0.1', 0), Handler)

    def info(self):
        return {'pid': os.getpid(), 'host': '127.0.0.1', 'port': self.server_address[1],
                'busy': self.job_lock.locked()}

    def publish(self):
        self.descriptor.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.descriptor, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({**self.info(), 'token': self.token}, stream)
        self.descriptor.chmod(0o600)

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.shutdown()
        self.server_close()
        try:
            if json.loads(self.descriptor.read_text()).get('token') == self.token:
                self.descriptor.unlink()
        except (OSError, ValueError):
            pass


def start():
    import binaryninja as bn
    from PySide6.QtWidgets import QApplication

    if not bn.core_ui_enabled() or QApplication.instance() is None:
        raise RuntimeError('Start the bridge inside the initialized Binary Ninja UI.')
    holder = sys.modules.setdefault(HOLDER, ModuleType(HOLDER))
    previous = getattr(holder, 'server', None)
    if previous is not None and not previous.stopped:
        return previous.info()
    server = Bridge()
    holder.server = server
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1},
                              name='bnnotes-agent-bridge', daemon=True)
    thread.start()
    server.publish()
    bn.execute_on_main_thread_and_wait(lambda: QApplication.instance().aboutToQuit.connect(
        server.stop,
    ))
    return server.info()
