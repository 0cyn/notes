"""Explicit agent UI inspection over a persistent local bridge."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import host


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('windows', help='List visible native window IDs, titles and process IDs')
    commands.add_parser('status', help='List connected application bridges without activating them')
    attach = commands.add_parser('attach', help='Get the one-time bridge bootstrap command')
    attach.add_argument('--console', action='store_true',
                        help='Bootstrap once through the console, then restore foreground focus')
    stop = commands.add_parser('stop', help='Stop the selected bridge')
    stop.add_argument('--pid', type=int)
    capture = commands.add_parser('capture', help='Capture an existing app window')
    capture.add_argument('--window', type=int,
                         help='Capture a macOS window ID, including native chrome')
    capture.add_argument('--pid', type=int, help='Capture app client areas through the bridge')
    capture.add_argument('--timeout', type=float, default=60)
    run = commands.add_parser('run', help='Run a script defining run(session) inside the app')
    run.add_argument('script', type=Path)
    preview = commands.add_parser('preview', help='Create and photograph an isolated Notes UI')
    preview.add_argument('scene', choices=['notes', 'settings', 'search', 'hlil', 'tabs',
                                           'onboarding'])
    preview.add_argument('--width', type=int, default=900)
    preview.add_argument('--height', type=int, default=650)
    opening = commands.add_parser('open', help='Open or restart Binary Ninja')
    opening.add_argument('file', nargs='?', type=Path)
    opening.add_argument('--restart', action='store_true')
    opening.add_argument('--surface', choices=['view', 'pane'])
    for command in (capture, run, preview, opening):
        command.add_argument('--output', type=Path, help='Artifact directory (default: unique run)')
    for command in (run, preview, opening, attach):
        command.add_argument('--pid', type=int)
        command.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    if sys.platform != 'darwin' and (args.command in ('windows', 'open')
                                     or args.command == 'capture' and args.window is not None
                                     or args.command == 'attach' and args.console):
        parser.error('Native window control and automatic bootstrap currently require macOS.')
    if getattr(args, 'timeout', 1) <= 0:
        parser.error('--timeout must be positive')
    if args.command == 'preview' and min(args.width, args.height) <= 0:
        parser.error('Preview dimensions must be positive')
    try:
        if args.command == 'windows':
            result = host.windows()
        elif args.command == 'status':
            result = [host.rpc(endpoint, 'ping') for endpoint in host.connections()]
        elif args.command == 'attach':
            result = host.attach(args.pid, console=args.console, timeout=args.timeout)
        elif args.command == 'stop':
            result = host.rpc(host.endpoint_for(args.pid), 'stop')
        else:
            output = host.output_directory(args.output)
            if args.command == 'capture':
                result = (host.capture(args.window, output) if args.window is not None else
                          host.run(host.HERE / 'examples/current_windows.py', output,
                                   pid=args.pid, timeout=args.timeout))
            elif args.command == 'open':
                result = host.open_file(args.file, output, pid=args.pid, restart=args.restart,
                                        surface=args.surface, timeout=args.timeout)
            else:
                script = args.script if args.command == 'run' else host.HERE / 'scenes.py'
                options = ({'scene': args.scene, 'width': args.width, 'height': args.height}
                           if args.command == 'preview' else {})
                result = host.run(script, output, pid=args.pid, timeout=args.timeout,
                                  options=options)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        print(getattr(error, 'stderr', None) or str(error), file=sys.stderr)
        return 1
