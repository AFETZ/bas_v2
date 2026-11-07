#!/usr/bin/env python3
"""Local BAS operator UI using the existing run controller; bind is loopback only."""
from __future__ import annotations

import argparse
import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import bas

BODY_LIMIT = 1024 * 1024


class UIState:
    def __init__(self, root=ROOT, *, launcher=None, stopper=None):
        self.root = Path(root).resolve()
        self.token = secrets.token_urlsafe(32)
        self.launcher, self.stopper = launcher, stopper or bas.request_stop
        self.children = {}
        self.lock = threading.Lock()

    def path(self, text, *, artifacts=False):
        if not isinstance(text, str) or not text or ':' in text:
            raise ValueError('expected a relative workspace path')
        relative = Path(text.replace('\\', '/'))
        if relative.is_absolute() or any(part in ('.', '..') for part in relative.parts):
            raise ValueError('expected a relative workspace path without traversal')
        path = (self.root / relative).resolve()
        path.relative_to(self.root / 'runs' if artifacts else self.root)
        if not artifacts and (path.suffix.lower() != '.json' or any(part.startswith('.') for part in relative.parts)):
            raise ValueError('configuration must be a workspace JSON file')
        return path

    def config(self, text):
        path = self.path(text)
        config = bas.load_config(path, root=self.root)
        scene = self.path(config['environment']['scene'])
        return dict(config=config, scene=json.loads(scene.read_text(encoding='utf-8-sig')))

    def validate(self, payload):
        if not isinstance(payload, dict) or set(payload) - {'config', 'scene', 'run_id'} or not isinstance(payload.get('config'), dict):
            raise ValueError('expected config object and optional scene/run_id')
        config = copy.deepcopy(payload['config'])
        if not isinstance(config.get('environment'), dict):
            raise ValueError('environment must be an object')
        self.path(config['environment'].get('scene'))
        directory = self.root / 'runs/control/ui-inputs'
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='validate-', dir=directory) as temporary:
            folder = Path(temporary)
            if 'scene' in payload:
                if not isinstance(payload['scene'], dict):
                    raise ValueError('scene must be an object')
                scene = folder / 'scene.json'
                bas.write_json(scene, payload['scene'])
                config['environment']['scene'] = scene.relative_to(self.root).as_posix()
            path = folder / 'config.json'
            bas.write_json(path, config)
            resolved = bas.load_config(path, root=self.root)
        # A preview must not reference the deleted validation snapshot.
        if 'scene' in payload:
            resolved['environment']['scene'] = payload['config']['environment']['scene']
        return resolved

    def start(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('start requires a configuration object')
        run_id = bas.identifier(payload.get('run_id'))
        config = self.validate(payload)
        if sys.platform != 'linux' and self.launcher is None:
            raise ValueError('Запуск симуляции требует Linux; проверка и редактирование доступны на этом компьютере.')
        with self.lock:
            if any(child.poll() is None for child in self.children.values()):
                raise ValueError('Один запуск интерфейса уже активен; дождитесь его завершения.')
            if (self.root / 'runs/control' / run_id).exists() or (self.root / 'runs/native-radio-realtime' / run_id).exists():
                raise ValueError('Идентификатор опыта уже использован.')
            folder = self.root / 'runs/control/ui-inputs' / run_id
            folder.mkdir(parents=True, exist_ok=False)
            if 'scene' in payload:
                bas.write_json(folder / 'scene.json', payload['scene'])
                config['environment']['scene'] = (folder / 'scene.json').relative_to(self.root).as_posix()
            bas.write_json(folder / 'config.json', config)
            argv = [sys.executable, str(self.root / 'bas.py'), 'run', '--config', str(folder / 'config.json'), '--run-id', run_id]
            with (folder / 'controller.log').open('x') as log:
                try:
                    child = (self.launcher(argv, folder) if self.launcher else
                             subprocess.Popen(argv, cwd=self.root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
                except OSError as error:
                    log.write(f'Controller launch failed: {error}\n')
                    raise
            self.children[run_id] = child
        return dict(run_id=run_id, launcher_pid=child.pid, config_path=(folder / 'config.json').relative_to(self.root).as_posix())

    def status(self, run_id):
        bas.identifier(run_id)
        control = self.root / 'runs/control' / run_id
        raw = self.root / 'runs/native-radio-realtime' / run_id
        record = control / 'run.json'
        mission = raw / 'metrics/relay_experiment_summary.json'
        with self.lock:
            child = self.children.get(run_id)
        inputs = self.root / 'runs/control/ui-inputs' / run_id
        if not record.exists() and child is None and not inputs.is_dir():
            raise FileNotFoundError('Опыт не найден.')
        phase = raw / 'logs/current_phase.txt'
        log = control / 'runtime.log'
        if not log.exists():
            log = inputs / 'controller.log'
        tail = ''
        if log.exists():
            with log.open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 65536))
                tail = stream.read().decode('utf-8', errors='replace')
        artifacts = []
        for folder in (control, raw / 'metrics', raw / 'logs', inputs):
            if folder.exists():
                for path in sorted(folder.iterdir()):
                    if path.is_file() and len(artifacts) < 80:
                        resolved = self.path(path.relative_to(self.root).as_posix(), artifacts=True)
                        artifacts.append(dict(path=resolved.relative_to(self.root).as_posix(), bytes=resolved.stat().st_size))
        returncode = child.poll() if child is not None else None
        return dict(run_id=run_id, controller=json.loads(record.read_text()) if record.exists() else None,
                    mission=json.loads(mission.read_text()) if mission.exists() else None,
                    phase=phase.read_text().strip() if phase.exists() else None, log_tail=tail, artifacts=artifacts,
                    launcher_returncode=returncode,
                    launcher_state=('running' if returncode is None else 'exited') if child else 'not_observed')


def make_server(port=8765, *, state=None):
    state = state or UIState()
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def send(self, status, body, content_type='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', f"default-src 'none'; script-src 'nonce-{state.token}'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def guard(self, mutation=False):
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get_all('Host') != [host]:
                self.send(403, dict(error='Недопустимый Host. Откройте адрес 127.0.0.1 из терминала.'))
                return False
            if mutation and (self.headers.get_all('Origin') != ['http://' + host]
                    or len(self.headers.get_all('X-BAS-Token', [])) != 1
                    or not hmac.compare_digest(self.headers.get('X-BAS-Token', '').encode(), state.token.encode())):
                self.send(403, dict(error='Неверный источник запроса или токен интерфейса.'))
                return False
            return True

        def do_GET(self):
            if not self.guard():
                return
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == '/':
                    page = (ROOT / 'web/operator.html').read_text(encoding='utf-8').replace('__TOKEN__', state.token)
                    self.send(200, page.encode(), 'text/html; charset=utf-8')
                elif url.path == '/api/bootstrap':
                    self.send(200, dict(token=state.token, can_launch=sys.platform == 'linux' or state.launcher is not None,
                                        **state.config('runtime.json')))
                elif url.path == '/api/config':
                    self.send(200, state.config(query.get('path', ['runtime.json'])[0]))
                elif url.path == '/api/status':
                    self.send(200, state.status(query.get('run_id', [''])[0]))
                elif url.path == '/api/runs':
                    folder = state.root / 'runs/control'
                    names = {path.parent.name for path in folder.glob('*/run.json')} if folder.exists() else set()
                    names.update(path.parent.name for path in (folder / 'ui-inputs').glob('*/config.json'))
                    with state.lock:
                        names.update(state.children)
                    self.send(200, dict(run_ids=sorted(names, reverse=True)[:100]))
                elif url.path == '/artifact':
                    path = state.path(query.get('path', [''])[0], artifacts=True)
                    if not path.is_file():
                        raise FileNotFoundError('Артефакт не найден.')
                    with path.open('rb') as stream:
                        size = stream.seek(0, 2)
                        stream.seek(0)
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Disposition', 'attachment; filename="artifact.bin"')
                        self.send_header('Content-Length', str(size))
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.end_headers()
                        while size > 0:
                            chunk = stream.read(min(size, 65536))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            size -= len(chunk)
                else:
                    self.send(404, dict(error='Страница не найдена.'))
            except (ValueError, TypeError, KeyError, RecursionError) as error:
                self.send(400, dict(error=str(error)))
            except FileNotFoundError as error:
                self.send(404, dict(error=str(error)))
            except OSError as error:
                self.send(400, dict(error=str(error)))

        def do_POST(self):
            if not self.guard(mutation=True):
                return
            try:
                if self.headers.get_all('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    raise ValueError('one Content-Length and no Transfer-Encoding required')
                size = int(self.headers.get('Content-Length', '-1'))
                if not 0 < size <= BODY_LIMIT:
                    self.send(413, dict(error='Тело запроса должно быть от 1 байта до 1 МиБ.'))
                    return
                if len(self.headers.get_all('Content-Type', [])) != 1 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    self.send(415, dict(error='Требуется application/json.'))
                    return
                payload = json.loads(self.rfile.read(size), object_pairs_hook=bas.unique_json_fields,
                                     parse_constant=bas.reject_json_constant)
                if self.path == '/api/validate':
                    self.send(200, dict(valid=True, config=state.validate(payload)))
                elif self.path == '/api/start':
                    self.send(202, state.start(payload))
                elif self.path == '/api/stop':
                    if not isinstance(payload, dict) or set(payload) != {'run_id'}:
                        raise ValueError('stop requires run_id only')
                    run_id = bas.identifier(payload['run_id'])
                    state.stopper(run_id)
                    self.send(202, dict(run_id=run_id, stop_requested=True))
                else:
                    self.send(404, dict(error='Операция не найдена.'))
            except (ValueError, TypeError, KeyError, OSError, RecursionError) as error:
                self.send(400, dict(error=str(error)))

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true', required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('port must be 0..65535')
    server = make_server(args.port)
    print(f'BAS operator: http://127.0.0.1:{server.server_port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
