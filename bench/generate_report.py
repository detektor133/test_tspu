#!/usr/bin/env python3
"""Generate report.md and SVG charts STRICTLY from field CSVs. No invented data."""
import csv
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import statistics
import sys

ORDER = ["VLESS REALITY", "VLESS XHTTP", "Hysteria2", "TUIC",
         "Shadowsocks", "Trojan", "AnyTLS"]


def read_csv(path):
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def when(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def resources_for(row, resources):
    start = when(row.get("started_utc"))
    end = when(row.get("ended_utc"))
    if start is None or end is None:
        return None
    points = [r for r in resources if
              (t := when(r.get("timestamp_utc"))) is not None and start <= t <= end]
    if not points:
        return None
    return {
        "cpu_avg": statistics.mean(num(r["mihomo_cpu_pct"]) for r in points),
        "cpu_peak": max(num(r["mihomo_cpu_pct"]) for r in points),
        "rss_peak": max(num(r["mihomo_rss_mib"]) for r in points),
        "mui_cpu_peak": max(num(r["mui_cpu_pct"]) for r in points),
        "mui_rss_peak": max(num(r["mui_rss_mib"]) for r in points),
    }


def chart(path, title, values, unit):
    # Static standalone SVG with data labels, no JS and no external resources.
    width = 920
    height = 80 + len(values) * 45
    maximum = max([value for _, value in values] + [0.01])
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (width, height, width, height),
        '<rect width="100%" height="100%" fill="#fbfcfe"/>',
        '<text x="20" y="32" font-family="Arial,sans-serif" font-size="18" font-weight="bold" fill="#172554">%s</text>' % html.escape(title),
    ]
    for n, (name, value) in enumerate(values):
        y = 53 + n * 45
        barwidth = int(value / maximum * 505)
        parts.extend([
            '<text x="20" y="%d" font-family="Arial,sans-serif" font-size="13" fill="#1f2937">%s</text>' % (y + 18, html.escape(name)),
            '<rect x="230" y="%d" width="505" height="23" fill="#e5e7eb" rx="4"/>' % y,
            '<rect x="230" y="%d" width="%d" height="23" fill="#2563eb" rx="4"/>' % (y, barwidth),
            '<text x="750" y="%d" font-family="Arial,sans-serif" font-size="13" fill="#111827">%.2f %s</text>' %
            (y + 17, value, html.escape(unit)),
        ])
    parts.append('</svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


def cell(value, precision=1):
    return ("%.{}f".format(precision) % value) if value is not None else "—"


def generate(folder):
    folder = Path(folder)
    rows = read_csv(folder / "measurements.csv")
    resources = read_csv(folder / "server-resources.csv")
    manifest_path = folder / "environment.json"
    meta = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    network = meta.get("network_label_user_declared") or "не указана"
    date = meta.get("date_utc") or "не установлена"
    server = meta.get("vps_ipv4") or "не указан"
    reported = []
    by_label = {}
    for label in ORDER:
        group = [r for r in rows if r.get("protocol") == label]
        tested = [r for r in group if r.get("status") in ("ok", "failed")]
        by_label[label] = tested
        if not tested:
            reason = group[0].get("status", "не измерялся") if group else "не измерялся"
            reported.append("| %s | — | — | — | — | — | — | %s |" % (label, reason))
            continue
        for r in tested:
            res = resources_for(r, resources)
            reported.append("| %s | %s | %s/%s | %s | %s | %s | %s | %s |" %
                            (label, r.get("concurrency", "—"), r.get("requests_ok", "0"),
                             r.get("requests_total", "0"),
                             cell(num(r.get("throughput_mib_s")), 3),
                             cell(num(r.get("latency_p95_ms"))),
                             cell(res["cpu_peak"] if res else None),
                             cell(res["rss_peak"] if res else None),
                             r.get("status", "?")))
    totals = {label: sum(int(num(r.get("requests_total"))) for r in group)
              for label, group in by_label.items()}
    successful = {label: sum(int(num(r.get("requests_ok"))) for r in group)
                  for label, group in by_label.items()}
    transferred = {label: sum(int(num(r.get("bytes_ok"))) for r in group)
                   for label, group in by_label.items()}
    good = [x for label in ORDER for x in by_label[label] if x.get("status") == "ok"]
    throughput = [(label, max((num(r.get("throughput_mib_s")) for r in by_label[label]), default=0.0))
                  for label in ORDER]
    latency = [(label, min((num(r.get("latency_p95_ms")) for r in by_label[label]
                            if r.get("status") == "ok"), default=0.0))
               for label in ORDER]
    # Zero on a chart means "no successful observation" rather than a measured 0 ms.
    charts = folder / "charts"
    charts.mkdir(exist_ok=True)
    chart(charts / "throughput.svg", "Максимальная наблюдаемая скорость по уровням", throughput, "МиБ/с")
    chart(charts / "latency.svg", "Минимальная наблюдаемая p95 по уровням (0 = нет данных)", latency, "мс")
    total_bytes = sum(transferred.values())
    all_requests = sum(totals.values())
    all_ok = sum(successful.values())
    absent = [label for label in ORDER if totals[label] == 0]
    notes = []
    if not rows:
        notes.append("Измерений нет: скрипт не успел создать ни одного результата.")
    if absent:
        notes.append("Не измерены или недоступны профили: " + ", ".join(absent) + ".")
    if not resources:
        notes.append("CPU/RAM на VPS отсутствуют: SSH-монитор не передал CSV.")
    elif good and not any(resources_for(r, resources) for r in good):
        notes.append("Время телефона и VPS не совпало: метрики CPU/RAM не удалось привязать к запросам.")
    if meta.get("error"):
        notes.append("Тест завершён с проблемой: " + str(meta["error"])[:200] + ".")
    notes.extend([
        "Тип доступа к сети указал пользователь; скрипт не доказывает, что Android реально использовал именно эту сеть.",
        "Проверен трафик через туннель к loopback-HTTP на собственном VPS; это не скорость стороннего сайта.",
        "TTFB и полное время ответа не равны сетевому RTT. Отдельный RTT не измерялся.",
        "Использование Mihomo и на сервере, и на клиенте ограничивает переносимость результатов на иные реализации.",
        "Один тайм-аут или падение скорости не доказывает вмешательство ТСПУ. Для таких выводов нужны повторные испытания и контрольные сети.",
        "Байты учитываются для успешных HTTP-ответов; ошибочные подключения и заголовки дают дополнительный неучтённый трафик.",
    ])
    summary = "\n".join("| %s | %d/%d | %.1f |" %
                        (label, successful[label], totals[label],
                         transferred[label] / (1024 * 1024))
                        for label in ORDER)
    details = "\n".join(reported)
    limits = "\n".join("- " + note for note in notes)
    status = "Полный прогон" if len(good) == len([r for r in rows if r.get("status") in ("ok", "failed")]) and not absent and not meta.get("error") else "Неполный прогон"
    document = """# Автоматический отчёт о полевом тестировании AnyModel

**Статус:** {status}.

## Условия эксперимента

- Время начала (UTC): {date}
- Сеть: {network} (**метка введена вручную**)
- Сервер: {server}
- Клиент: Android/Termux, {client}
- Полезная нагрузка одного запроса: {size} байт
- Уровни параллельных HTTP-запросов: {levels}
- Плановое время одного уровня: {seconds} с
- Верхняя граница отправленных ответов: {budget} МиБ (считая полный ответ даже при ошибке проверки)
- Успешных запросов за весь тест: {ok}/{count}
- Подтверждённый объём успешных HTTP-ответов: {mib:.2f} МиБ

Тестовый HTTP-сервис работал **только на loopback VPS**, запросы отправлялись с Android через выбранный
профиль Mihomo. Подключения не использовали USB-модем, общий VPN на ПК или публикацию origin наружу.
Между этапами ядро Mihomo на телефоне переключало маршрутизацию через локальный REST API.

## Сравнение протоколов

| Протокол | Успешные/все запросы | Подтверждённый трафик, МиБ |
|---|---:|---:|
{summary}

| Протокол | Потоки | OK/все | МиБ/с | p95 полного ответа, мс | CPU Mihomo peak, % ядра | RSS peak, МиБ | Статус |
|---|---:|---:|---:|---:|---:|---:|---|
{details}

100% CPU = одно занятое логическое ядро; это не процент всех ядер VPS.
Строки без успешных запросов не дают достоверного p95; их нулевые значения скорости/задержки следует читать как отсутствие успешного измерения, а не как нулевую реальную задержку.

![Скорость](charts/throughput.svg)

![Задержка](charts/latency.svg)

CSV с первичными данными: measurements.csv, server-resources.csv.
В каждом уровне имеются started_utc/ended_utc для привязки метрик VPS.

## Инженерный выбор транспорта и гипотезы

**Основной проверяемый кандидат — VLESS + REALITY + Vision (TCP/443).**
REALITY имитирует свойства обычного TLS-рукопожатия с выбранным SNI; это не
гарантия отсутствия идентификации DPI. Проверки доступа к выбранной цели с VPS
не заменяют тест в конкретной сети РФ.

Контрольные альтернативы: VLESS + XHTTP + TLS (HTTP-подобный транспорт)
и Hysteria2 (QUIC/UDP). Другие четыре протокола тоже измеряются,
но их наличие само по себе не свидетельствует об обходе фильтрации.
Предполагаемые ограничения: блокировка IP/ASN, политика к SNI, активное
зондирование, сигнатуры и поведенческий анализ, ограничения UDP в мобильных сетях.
Эти гипотезы требуют проверки на нескольких провайдерах и в разные часы.

Источники для **интерпретации, не для автоматических численных выводов**:
- OONI, Russia censorship (2024): https://ooni.org/post/2024-russia-report
- Human Rights Watch, Disrupted, Throttled, and Blocked (2025): https://www.hrw.org/report/2025/07/30/disrupted-throttled-and-blocked/state-censorship-control-and-increasing-isolation
- XTLS/REALITY: https://github.com/XTLS/REALITY
- Mihomo: https://github.com/MetaCubeX/mihomo
- m-ui: https://github.com/RomanovCaesar/m-ui

## Наблюдения, ограничения и недостающие доказательства

{limits}

Для вывода о сетевой фильтрации сопоставьте результаты SberMobile и Ростелекома
при одинаковых параметрах и повторите проблемные попытки. Не выдавайте локальные
CI-тесты и фрагментарные замеры за доказательство устойчивости к ТСПУ.

## Для завершения сдачи (не генерируется автоматически)

- Подтвердить провайдера/ASN, маршрут до VPS и физическую сеть телефона.
- Добавить обоснование хостинга, стоимость и географию VPS.
- Записать **фактическое** суммарное время работы над заданием.
- Передать рабочую конфигурацию для Hiddify/v2rayNG/Streisand **приватно**:
  ни QR, ни токены подписок нельзя помещать в этот публичный отчёт.
- Оставить VPS доступным **7 дней после отправки**.

Этот документ создан автоматически из результатов, а не написан по
предположительным числам. Для воспроизведения см. bench/README.md.
""".format(
        status=status, date=date, network=network, server=server,
        client=meta.get("client_version", "Mihomo")[:100],
        size=meta.get("payload_bytes", 0), levels=meta.get("levels", []),
        seconds=meta.get("seconds_per_level", "—"),
        budget=meta.get("max_mib", "—"), ok=all_ok, count=all_requests,
        mib=total_bytes / (1024 * 1024), summary=summary,
        details=details, limits=limits,
    )
    (folder / "report.md").write_text(document, encoding="utf-8")
    return folder / "report.md"


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Использование: python3 bench/generate_report.py ПАПКА_РЕЗУЛЬТАТОВ")
    print(generate(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
