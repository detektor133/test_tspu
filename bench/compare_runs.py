#!/usr/bin/env python3
"""Compare two or more real mobile field runs without inventing observations."""
import argparse
import csv
import json
from pathlib import Path
from generate_report import chart, ORDER


def rows_in(path):
    file = path / "measurements.csv"
    if not file.exists():
        raise SystemExit("Нет файла измерений: " + str(file))
    with file.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def combine(folders, output):
    folders = [Path(p).expanduser().resolve(strict=True) for p in folders]
    if len(folders) < 2:
        raise SystemExit("Нужны хотя бы два отдельных прогона разных сетей")
    datasets = {}
    metadata = {}
    for folder in folders:
        info = json.loads((folder / "environment.json").read_text())
        label = info.get("network_label_user_declared", "")
        if not label or label in datasets:
            raise SystemExit("У каждой серии должна быть своя метка сети")
        datasets[label] = rows_in(folder)
        metadata[label] = info
    # Проверяем одинаковую методику. Не приводим разнородные уровни к
    # фиктивным общим цифрам: каждый факт показываем только в своей ячейке.
    def index(rows):
        return {(r["protocol"], int(float(r["concurrency"]))): r
                for r in rows if r.get("protocol") in ORDER
                and r.get("status") in ("ok", "failed", "budget_limited", "insufficient_samples")
                and r.get("concurrency")}
    indexed = {net: index(rows) for net, rows in datasets.items()}
    levels = sorted({level for data in indexed.values() for _, level in data})
    header = "| Протокол | Потоки | " + " | ".join(
        net + ": OK/все · МиБ/с · p95, мс" for net in datasets) + " |"
    delimiter = "|---|---:|" + "|".join(["---:" for _ in datasets]) + "|"
    table = [header, delimiter]
    for protocol in ORDER:
        for level in levels:
            columns = [protocol, str(level)]
            for net in datasets:
                r = indexed[net].get((protocol, level))
                if not r:
                    columns.append("—")
                    continue
                if r.get("status") != "ok":
                    columns.append("%s/%s · не сопоставимо (%s)" %
                                   (r.get("requests_ok", 0), r.get("requests_total", 0),
                                    r.get("status", "неизвестно")))
                else:
                    columns.append("%s/%s · %s · %s" %
                                   (r.get("requests_ok", 0), r.get("requests_total", 0),
                                    r.get("throughput_mib_s", "—"),
                                    r.get("latency_p95_ms") or "—"))
            table.append("| " + " | ".join(columns) + " |")
    # Для графика выбираем только совместимый уровень параллельности,
    # предпочтительно 8. Отсутствующие успешные измерения не сравниваем.
    methodologies = {(m.get('seconds_per_level'), m.get('payload_bytes'))
                     for m in metadata.values()}
    compatible = len(methodologies) == 1
    shared = set.intersection(*[
        set(level for (protocol, level), r in idx.items() if r.get('status') == 'ok')
        for idx in indexed.values()
    ]) if indexed else set()
    chosen = (8 if 8 in shared else (min(shared) if shared else None)) if compatible else None
    plot = []
    if chosen is not None:
        for protocol in ORDER:
            # Never chart an unpaired protocol as an apparent network comparison.
            if not all(indexed[net].get((protocol, chosen), {}).get('status') == 'ok'
                       for net in datasets):
                continue
            for net in datasets:
                r = indexed[net][(protocol, chosen)]
                plot.append((protocol + " · " + net,
                             float(r.get("throughput_mib_s") or 0)))
    output = Path(output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    note = (
        "Сети помечены вручную и автоматически не верифицированы. "
        "Разница скорости не доказывает вмешательство ТСПУ. "
        "В сравнительные цифры и график входят только полные по времени уровни с >=20 "
        "успешными ответами (статус ok). Раннее исчерпание бюджета и малая выборка "
        "показаны как несопоставимые. Для графика требуется одинаковая длительность "
        "и размер ответа. Мобильный сигнал, маршрут и часы испытаний могли отличаться."
    )
    entries = "\n".join(
        "- %s: UTC %s; телефон %s; ошибки запуска: %s" %
        (net, meta.get("date_utc", "?"),
         meta.get("client_version", "?")[:120],
         meta.get("error") or "не зафиксированы")
        for net, meta in metadata.items()
    )
    graphic = ""
    if plot:
        svg = output.with_name(output.stem + "-speed.svg")
        chart(svg, "Сопоставимая скорость, %s параллельных потоков" % chosen,
              plot, "МиБ/с")
        graphic = "\n![Сравнение скорости](%s)\n" % svg.name
    text = (
        "# Полевое сравнение сетей: реальные измерения\n\n"
        + entries + "\n\n"
        + "## По каждому протоколу и уровню параллельности\n\n"
        + "\n".join(table) + "\n"
        + graphic + "\n"
        + "## Допущения и ограничения\n\n"
        + note + "\n\n"
        + "Подробная методика, CPU/RAM и исходные CSV находятся в отдельных "
          "папках прогонов. Если нет одинакового уровня нагрузки или успехов "
          "в обоих прогонах, сравнимое число не рассчитывается.\n"
    )
    output.write_text(text, encoding="utf-8")
    return output


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("folders", nargs="+", help="каталоги полевых результатов")
    p.add_argument("--output", required=True, help="куда сохранить итоговый Markdown")
    args = p.parse_args()
    print(combine(args.folders, args.output))
