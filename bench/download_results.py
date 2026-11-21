#!/usr/bin/env python3
"""One-command safe result archive and browser download; no Android storage perms.

Serves only a generated allowlist archive on 127.0.0.1, never home/secret paths.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import tarfile
from urllib.parse import quote

from export_results import ALLOWED


def create_archive(folder):
    folder = Path(folder).expanduser().resolve(strict=True)
    if not ((folder / "report.md").is_file()
            and (folder / "environment.json").is_file()):
        raise ValueError("В каталоге нет готового отчёта")
    root = Path.home() / "test-tspu-results" / "safe-archives"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", folder.name):
        raise ValueError("Некорректное имя каталога прогона")
    outfile = root / (folder.name + "-safe.tar.gz")
    with tarfile.open(outfile, "w:gz") as tar:
        for part in ALLOWED:
            src = folder / part
            if not src.is_file() or src.is_symlink():
                continue
            if folder not in src.resolve().parents:
                raise ValueError("Обнаружена ссылка за пределы каталога")
            tar.add(src, arcname=folder.name + "/" + part, recursive=False)
    outfile.chmod(0o600)
    return outfile


def serve(outfile, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/" + quote(outfile.name):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition", 'attachment; filename="' + outfile.name + '"')
            self.send_header("Content-Length", str(outfile.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with outfile.open("rb") as fp:
                while block := fp.read(65536):
                    self.wfile.write(block)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print("Открой в браузере ЭТОГО ЖЕ телефона:")
        print("http://127.0.0.1:%d/%s" % (port, quote(outfile.name)))
        print("Архив содержит только разрешённые CSV/JSON/отчёт; секретов нет.")
        print("После скачивания нажми Ctrl+C в Termux.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Безопасный архив + скачивание через Chrome")
    parser.add_argument("folder", help="каталог test-tspu-results/...")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port 1024..65535")
    archive = create_archive(args.folder)
    print("Архив создан: %s (%d байт)" % (archive, archive.stat().st_size))
    serve(archive, args.port)
