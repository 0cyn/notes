"""Local bridge client, optional one-time bootstrap, and native window capture."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
EXECUTABLE = '/Applications/Binary Ninja.app/Contents/MacOS/binaryninja'
BRIDGES = ROOT / '.agent-artifacts/bridges'


def activate(pid):
    subprocess.run(['swift', str(HERE / 'activate.swift'), str(pid)],
                   capture_output=True, text=True, timeout=30, check=True)


def control(operation, pid=0, *args, timeout=30):
    if operation != 'inspect':
        activate(pid)
    result = subprocess.run(
        ['osascript', str(HERE / 'control.applescript'), operation, str(pid), *map(str, args)],
        capture_output=True, text=True, timeout=timeout, check=True,
    )
    return result.stdout.strip()


def windows():
    result = subprocess.run(['swift', str(HERE / 'windows.swift')], capture_output=True,
                            text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def process(pid=None, *, required=True):
    # A running application may have only minimized/hidden windows. Do not start
    # a duplicate process just because screen capture has nothing to list.
    inspected = control('inspect', pid or 0).splitlines()
    if inspected:
        return int(inspected[0])
    if required or pid is not None:
        raise RuntimeError('Binary Ninja is not running for the selected process.')
    return None


def output_directory(path=None):
    directory = (Path(path).expanduser().resolve() if path else
                 ROOT / '.agent-artifacts' / f'{time.strftime("%Y%m%d-%H%M%S")}-{uuid4().hex[:8]}')
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def rpc(endpoint, operation, *, timeout=5, **parameters):
    message = {'token': endpoint['token'], 'operation': operation, **parameters}
    with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=timeout) as connection:
        connection.sendall(json.dumps(message).encode() + b'\n')
        with connection.makefile('rb') as stream:
            response = json.loads(stream.readline(65536))
    if 'error' in response:
        raise RuntimeError(response['error'])
    return response


def connections():
    result = []
    for path in BRIDGES.glob('*.json'):
        try:
            endpoint = json.loads(path.read_text())
            info = rpc(endpoint, 'ping', timeout=0.5)
            if info['pid'] == endpoint['pid']:
                result.append(endpoint)
        except (OSError, RuntimeError, ValueError, KeyError):
            continue
    return result


def endpoint_for(pid=None):
    candidates = [item for item in connections() if pid is None or item['pid'] == pid]
    if len(candidates) != 1:
        raise RuntimeError('Select one running agent bridge with --pid. '
                           'Use status, or attach to get the one-time bootstrap command.')
    return candidates[0]


def attach(pid=None, *, console=False, timeout=15):
    command = f"__import__('runpy').run_path({ascii(str(HERE / 'bridge.py'))})['start']()"
    if not console:
        return {'bootstrap': command, 'instruction': 'Run once in Binary Ninja’s Python console.'}
    pid = process(pid)
    previous = subprocess.run(['swift', str(HERE / 'activate.swift'), '--frontmost'],
                              capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    try:
        control('console', pid, command)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for endpoint in connections():
                if endpoint['pid'] == pid:
                    return rpc(endpoint, 'ping')
            time.sleep(0.1)
        raise TimeoutError('Bridge did not acknowledge startup. '
                           'Run the bootstrap command manually.')
    finally:
        if previous and int(previous) not in (0, pid):
            subprocess.run(['swift', str(HERE / 'activate.swift'), '--restore', previous],
                           capture_output=True, text=True, timeout=30, check=False)


def capture(window_id, directory):
    window = next((row for row in windows() if row['id'] == window_id), None)
    if window is None:
        raise RuntimeError(f'Window {window_id} is not visible. Run windows again.')
    output = directory / f'window-{window_id}.png'
    subprocess.run(['/usr/sbin/screencapture', '-x', '-o', f'-l{window_id}', str(output)],
                   capture_output=True, text=True, timeout=20, check=True)
    if not output.is_file() or not output.stat().st_size:
        raise RuntimeError('Screen capture did not produce an image.')
    result = {'window': window, 'images': [str(output)]}
    (directory / 'capture.json').write_text(json.dumps(result, indent=2))
    return result


def run(script, directory, *, pid=None, timeout=60, options=None):
    endpoint = endpoint_for(pid)
    pid = endpoint['pid']
    run_id = uuid4().hex
    request = directory / f'{run_id}.request.json'
    receipt = directory / f'{run_id}.result.json'
    payload = {'run_id': run_id, 'pid': pid, 'script': str(Path(script).resolve(strict=True)),
               'output': str(directory), 'receipt': str(receipt), 'options': options or {}}
    request.write_text(json.dumps(payload, indent=2))
    accepted = rpc(endpoint, 'run', request=str(request))
    if accepted.get('accepted') != run_id or accepted.get('pid') != pid:
        raise RuntimeError('Bridge did not acknowledge this request.')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if receipt.exists():
            result = json.loads(receipt.read_text())
            if result.get('run_id') != run_id or result.get('pid') != pid:
                raise RuntimeError('UI receipt does not match the requested run/process.')
            if result['status'] in ('complete', 'failed'):
                if result['status'] == 'failed':
                    raise RuntimeError(f'UI script failed; see {receipt}\n{result["error"]}')
                return result
        time.sleep(0.1)
    raise TimeoutError(f'UI run did not finish within {timeout}s; receipt: {receipt}. '
                       'The command may still be running; it has not been resubmitted.')


def open_file(path, directory, *, pid=None, restart=False, surface=None, timeout=60):
    existing = process(pid, required=False)
    if path is None:
        if existing is None:
            raise RuntimeError('Supply a file when Binary Ninja is not running.')
        activate(existing)
        inspected = control('inspect', existing).splitlines()
        url = urlsplit(inspected[1] if len(inspected) > 1 else '')
        if url.scheme != 'file' or url.netloc not in ('', 'localhost'):
            raise RuntimeError('No active local file. Supply a binary or BNDB path.')
        path = unquote(url.path)
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise RuntimeError(f'Expected a binary or BNDB file: {path}')
    if restart and existing is not None:
        control('save', existing, timeout=timeout)
        os.kill(existing, signal.SIGKILL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(existing, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            raise TimeoutError(f'Process {existing} did not exit.')
    if existing is None or restart:
        log = directory / 'launch.log'
        with log.open('w') as stream:
            child = subprocess.Popen([EXECUTABLE, '--flush-lock', str(path)],
                                     stdin=subprocess.DEVNULL, stdout=stream,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(row['pid'] == child.pid for row in windows()):
                existing = child.pid
                break
            time.sleep(0.2)
        else:
            raise TimeoutError(f'Binary Ninja did not open a window; see {log}')
        attach(existing, console=True, timeout=timeout)
    # Use the existing application's UI API, rather than starting a second
    # process to open another file. Opening a file can create a new UIContext.
    return run(HERE / 'open_file.py', directory, pid=existing, timeout=timeout,
               options={'file': str(path), 'surface': surface, 'timeout': timeout})
