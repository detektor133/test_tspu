#!/usr/bin/env python3
"""Локальный endpoint на VPS для тестов только через авторизованный туннель."""
import argparse
import hashlib
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    ap = argparse.ArgumentParser(description='Контрольный HTTP endpoint (только 127.0.0.1)')
    ap.add_argument('--port', type=int, default=18180)
    ap.add_argument('--size', type=int, default=1048576, help='размер полезной нагрузки в байтах')
    args = ap.parse_args()
    if not (1024 <= args.size <= 16 * 1024 * 1024) or not (1024 <= args.port < 65536):
        ap.error('size: 1 КиБ..16 МиБ; port: 1024..65535')
    payload = os.urandom(args.size)
    checksum = hashlib.sha256(payload).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split('?', 1)[0] != '/payload':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-SHA256', checksum)
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt, *args):
            return  # Не записываем клиентские адреса и параметры запросов

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'HTTP origin на 127.0.0.1:{args.port}; размер={args.size} байт; sha256={checksum}', flush=True)
    print('Порт привязан к loopback, не публикуйте его наружу.', flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()