"""Install if necessary, start the local server, then open the working application."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser

ROOT = Path(__file__).resolve().parent


def choose_port():
    for port in range(8000, 8021):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError('Ports 8000-8020 are busy. Close an old server and try again.')


def serve(python, port, *, open_browser=True):
    url = f'http://127.0.0.1:{port}'
    server = subprocess.Popen([str(python), '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT)
    # Local readiness checks must not be routed through an Internet proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError('The server stopped. See the error above.')
            try:
                for path in ['/api/health', '/static/style.css', '/static/app.js', '/static/auth.css', '/static/auth.js']:
                    with opener.open(url + path, timeout=1) as response:
                        if response.status != 200:
                            raise RuntimeError('Application files could not be loaded.')
                break
            except (OSError, urllib.error.URLError):
                time.sleep(.2)
        else:
            raise RuntimeError('The server did not become ready in 30 seconds.')
        print(f'\nOpen {url}/login in your browser. Keep this window open. Press Ctrl+C to stop.', flush=True)
        if open_browser:
            webbrowser.open(url + '/login')
        server.wait()
        if server.returncode:
            raise RuntimeError('The server stopped unexpectedly.')
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def main():
    if sys.version_info < (3, 12):
        raise RuntimeError('Please install Python 3.12 or later.')
    python = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        print('Preparing Python environment...', flush=True)
        venv.EnvBuilder(with_pip=True).create(ROOT / '.venv')
    probe = subprocess.run([str(python), '-c', 'import fastapi, uvicorn, openpyxl'], capture_output=True)
    if probe.returncode:
        print('Installing dependencies (Internet connection required)...', flush=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.lock')], cwd=ROOT, check=True)
    subprocess.run([str(python), '-m', 'app.manage_admin', '--if-needed'], cwd=ROOT, check=True)
    serve(python, choose_port())


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nServer stopped.')
    except Exception as error:
        print(f'\nStartup failed: {error}', file=sys.stderr)
        sys.exit(1)
