#!/usr/bin/env python3
"""Export ONLY non-secret results into Android Downloads for adb pull."""
import argparse
from pathlib import Path
import shutil

ALLOWED = ("report.md", "environment.json", "measurements.csv",
           "request-samples.csv", "negative-controls.json", "time-windows.csv",
           "server-resources.csv", "youtube-results.csv", "charts/throughput.svg", "charts/latency.svg",
           "charts/time-windows.svg")


def export(source, downloads):
    source = Path(source).expanduser().resolve(strict=True)
    if not (source / "report.md").is_file() or not (source / "environment.json").is_file():
        raise SystemExit("Нет готового отчёта. Сначала запустите phone.py run.")
    dest = Path(downloads).expanduser() / "test-tspu-results" / source.name
    if dest.exists():
        raise SystemExit("Экспорт уже существует: " + str(dest))
    dest.mkdir(parents=True, mode=0o700)
    for file in ALLOWED:
        src = source / file
        if not src.is_file() or src.is_symlink():
            continue
        out = dest / file
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
    print("Скопированы только обезличенные результаты: " + str(dest))
    print("Далее на ПК: adb pull /sdcard/Download/test-tspu-results/")
    print("private-mihomo.yaml, URL подписки и ключи НЕ экспортировались.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("source", help="каталог результатов phone.py")
    p.add_argument("--downloads", default="~/storage/downloads")
    a = p.parse_args()
    export(a.source, a.downloads)
