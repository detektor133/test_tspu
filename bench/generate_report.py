#!/usr/bin/env python3
"""Generate report.md and SVG charts STRICTLY from field CSVs. No invented data."""
import csv
from datetime import datetime, timezone
import html
import json
import re
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


def attach_evidence(folder, rows):
    """Reject rows whose packet proof, controls or raw request log is absent.

    This intentionally does not upgrade legacy field CSVs on regeneration.
    """
    folder = Path(folder)
    samples = read_csv(folder / "request-samples.csv")
    controls_path = folder / "negative-controls.json"
    try:
        controls = json.loads(controls_path.read_text())
        controls_passed = (controls.get("DIRECT") == "failed_as_expected"
                           and controls.get("BROKEN_SOCKS5") == "failed_as_expected")
    except (OSError, ValueError, AttributeError):
        controls_passed = False
    groups = {}
    ping_groups = {}
    invalid_groups = set()
    for sample in samples:
        key = (sample.get("protocol"), str(sample.get("concurrency")))
        if sample.get("kind") == "ping":
            ping_groups.setdefault(key, []).append(sample)
        elif sample.get("kind") == "download":
            groups.setdefault(key, []).append(sample)
        else:
            invalid_groups.add(key)
    for row in rows:
        key = (row.get("protocol"), str(row.get("concurrency")))
        part = groups.get(key, [])
        ping_part = ping_groups.get(key, [])
        try:
            nonces = [v.get("nonce", "") for v in part + ping_part]
            complete = (key not in invalid_groups
                        and all(re.fullmatch(r"[0-9a-f]{24}", n) for n in nonces)
                        and len(set(nonces)) == len(nonces)
                        and len(part) == int(row.get("requests_total") or 0)
                        and len(part) > 0
                        and sum(int(v.get("ok") or 0) for v in part) ==
                            int(row.get("requests_ok") or 0)
                        and sum(int(v.get("bytes_ok") or 0) for v in part) ==
                            int(row.get("bytes_ok") or 0)
                        and all(v.get("nonce") and len(v["nonce"]) == 24 for v in part)
                        and len(ping_part) ==
                            int(row.get("app_rtt_samples") or 0) +
                            int(row.get("app_rtt_failed") or 0)
                        and sum(int(v.get("ok") or 0) for v in ping_part) ==
                            int(row.get("app_rtt_samples") or 0)
                        and all(v.get("nonce") and len(v["nonce"]) == 24 for v in ping_part))
        except (TypeError, ValueError):
            complete = False
        row["raw_samples_complete"] = complete
        row["controls_passed"] = controls_passed
    return controls_passed


def valid_level(row):
    """Validated sustained throughput; p95 quality is checked independently.

    A completed interval with only a handful of HTTP downloads may still
    provide factual throughput; it must not invent a latency percentile.
    """
    try:
        return (row.get("status") == "ok"
                and row.get("mode") in ("load", "soak")
                and row.get("measurement_status") == "time_completed"
                and float(row.get("seconds_configured") or 0) > 0
                and float(row.get("seconds_elapsed") or 0) >= float(row["seconds_configured"]) - .01
                and int(float(row.get("requests_ok") or 0)) >= 20
                and int(float(row.get("peak_inflight") or 0)) >= int(float(row.get("concurrency") or 0))
                and row.get("route_status") in ("verified", "verified_with_noise")
                and int(float(row.get("route_packets") or 0)) > 0
                and row.get("raw_samples_complete") is True
                and row.get("controls_passed") is True)
    except (ValueError, TypeError, KeyError):
        return False


def valid_rtt(row):
    """p95 of under-load application ping; 20+ successes, separate from throughput."""
    return (valid_level(row)
            and int(num(row.get("app_rtt_samples"))) >= 20
            and row.get("app_rtt_p95_ms") not in (None, ""))


def metric(value, precision=1):
    if value is None or value == '':
        return '—'
    return cell(num(value), precision)


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
        "mihomo_present": any(num(r.get("mihomo_count")) > 0 for r in points),
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
    if not values:
        parts.append('<text x="20" y="67" font-family="Arial,sans-serif" '
                     'font-size="14" fill="#374151">Нет подтверждённых '
                     'данных для этого показателя</text>')
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
    controls_passed = attach_evidence(folder, rows)
    resources = read_csv(folder / "server-resources.csv")
    manifest_path = folder / "environment.json"
    meta = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    network = meta.get("network_label_user_declared") or "не указана"
    date = meta.get("date_utc") or "не установлена"
    server = meta.get("vps_ipv4") or "не указан"
    reported = []
    mode = meta.get("mode", "legacy")
    expected_protocols = meta.get("protocols_selected") or ORDER
    by_label = {}
    for label in ORDER:
        group = [r for r in rows if r.get("protocol") == label]
        tested = [r for r in group if r.get("concurrency") not in (None, "")]
        by_label[label] = tested
        if not tested:
            reason = group[0].get("status", "не измерялся") if group else "не измерялся"
            reported.append("| " + " | ".join([label] + ["—"] * 11 + [reason]) + " |")
            continue
        for r in tested:
            res = resources_for(r, resources)
            status_label = (r.get("status", "?") if r.get("status") != "ok" or valid_level(r)
                            else "unverified_legacy")
            reported.append("| " + " | ".join([
                label, str(r.get("concurrency") or "—"),
                str(r.get("requests_ok") or "0") + "/" +
                str(r.get("requests_total") or "0"),
                metric(r.get("seconds_elapsed"), 2) + "/" +
                metric(r.get("seconds_configured"), 2),
                metric(r.get("throughput_mib_s"), 3) if mode != "smoke" else "не измеряется",
                (metric(r.get("app_rtt_p50_ms")) + " / " +
                 metric(r.get("app_rtt_p95_ms"))) if mode != "smoke" else "не измеряется",
                (str(r.get("app_rtt_samples", 0)) + " / " +
                 str(r.get("app_rtt_failed", 0))) if mode != "smoke" else "—",
                metric(r.get("latency_p95_ms")) if mode != "smoke" else "не измеряется",
                str(r.get("route_port") or "—") + " (" +
                str(r.get("route_packets") or "0") + " пак.)",
                metric(r.get("protocol_cumulative_mib"), 2),
                cell(res["cpu_peak"] if res else None),
                cell(res["rss_peak"] if res else None),
                status_label,
            ]) + " |")
    totals = {label: sum(int(num(r.get("requests_total"))) for r in group)
              for label, group in by_label.items()}
    successful = {label: sum(int(num(r.get("requests_ok"))) for r in group)
                  for label, group in by_label.items()}
    transferred = {label: sum(int(num(r.get("bytes_ok"))) for r in group)
                   for label, group in by_label.items()}
    good = [x for label in ORDER for x in by_label[label] if valid_level(x)]
    good_rtt = [x for x in good if valid_rtt(x)]
    quality_issues = [x for label in expected_protocols for x in by_label[label]
                      if not valid_level(x)]
    max_verified_parallel = {
        label: max((int(num(r.get("concurrency"))) for r in by_label[label]
                    if valid_level(r) and resources_for(r, resources) is not None
                    and resources_for(r, resources)["mihomo_present"]), default=0)
        for label in ORDER
    }
    # Each bar represents one protocol AT ONE concurrency level.
    # Comparing maxima from unlike levels would quietly change the workload.
    throughput = [
        (label + " / " + str(r["concurrency"]) + " потоков",
         num(r.get("throughput_mib_s")))
        for label in expected_protocols for r in by_label[label] if valid_level(r)
    ]
    latency = [
        (label + " / " + str(r["concurrency"]) + " потоков",
         num(r.get("app_rtt_p95_ms")))
        for label in expected_protocols for r in by_label[label] if valid_rtt(r)
    ]
    # Do not display fake zero-height bars for unavailable measurements.
    charts = folder / "charts"
    charts.mkdir(exist_ok=True)
    chart(charts / "throughput.svg", "Скорость: только полные интервалы и достаточно выборок", throughput, "МиБ/с")
    chart(charts / "latency.svg", "HTTP ping под нагрузкой: p95 (0 = нет данных)", latency, "мс")
    total_bytes = sum(transferred.values())
    all_requests = sum(totals.values())
    all_ok = sum(successful.values())
    absent = [label for label in expected_protocols if totals[label] == 0]
    notes = []
    if not rows:
        notes.append("Измерений нет: скрипт не успел создать ни одного результата.")
    if absent:
        notes.append("Не измерены или недоступны профили: " + ", ".join(absent) + ".")
    if quality_issues and mode != "smoke":
        notes.append("Часть нагрузочных ступеней не прошла критерии полного интервала, "
                     "успешности или подтверждения маршрута. Они остаются в таблице, "
                     "но исключены из сравнения устойчивой пропускной способности.")
    if mode == "smoke":
        notes.append("Режим smoke проверяет только доступность, отрицательные контроли "
                     "и целостность. Скорость и p95 НЕ являются результатами нагрузочного "
                     "исследования, поэтому графики не строятся.")
    elif good and len(good_rtt) < len(good):
        notes.append("На отдельных полных ступенях нет >=20 успешных HTTP ping. "
                     "Пропускная способность подтверждена независимо; p95 остаётся "
                     "«—», что не обнуляет скорость.")
    if not controls_passed:
        notes.append("ОТРИЦАТЕЛЬНЫЕ КОНТРОЛИ НЕ ПОДТВЕРЖДЕНЫ: данные нельзя считать "
                     "аудированным тестом выбранных маршрутов.")
    if any(r.get("route_status") == "verified_with_noise" for r in rows):
        notes.append("На некоторых ступенях были единичные пакеты других тестовых портов: "
                     "они сохранены как предупреждение (verified_with_noise), не как "
                     "автоматическое опровержение наблюдаемого правильного порта.")
    if not (folder / "request-samples.csv").is_file():
        notes.append("Нет сырых данных каждого запроса: старые результаты "
                     "не считаются валидированным бенчмарком.")
    if resources and not any(num(r.get("mihomo_count")) > 0 for r in resources):
        notes.append("ВНИМАНИЕ: монитор не обнаружил серверный Mihomo (mihomo_count=0); "
                     "интерпретация CPU/RAM не подтверждена.")
    if not resources:
        notes.append("CPU/RAM на VPS отсутствуют: SSH-монитор не передал CSV.")
    elif good and not any(resources_for(r, resources) for r in good):
        notes.append("Время телефона и VPS не совпало: метрики CPU/RAM не удалось привязать к запросам.")
    if meta.get("error"):
        notes.append("Тест завершён с проблемой: " + str(meta["error"])[:200] + ".")
    notes.extend([
        "Тип доступа к сети указал пользователь; скрипт не доказывает, что Android реально использовал именно эту сеть.",
        "Проверен трафик через туннель к loopback-HTTP на собственном VPS; это не скорость стороннего сайта.",
        "HTTP /ping под одновременной нагрузкой измеряет прикладную задержку до origin; "
        "это не ICMP RTT. p95 загрузки 256 КиБ измеряется отдельно.",
        "budget_limited: бюджет кончился раньше таймера; скорость за фактический интервал "
        "не является устойчивой скоростью за заданные секунды.",
        "insufficient_samples / insufficient_rtt_samples: полный интервал, но менее 20 "
        "успешных загрузок или запросов /ping; "
        "для p95 требуется >=20, для p99 >=100. Недостаточные процентили показаны как «—».",
        "Присутствие route_ambiguous/no_ingress либо errors_observed не доказывает DPI; "
        "требуются независимые повторные проверки.",
        "Старые CSV без исходных HTTP ping и port evidence не признаются полноценно проверенными.",
        "insufficient_samples: до конца таймера дошли, но получено меньше 20 успешных ответов; "
        "для p95 требуется >=20, для p99 >=100. Недостаточные процентили показаны как «—».",
        "Пассивные счётчики порта VPS не аутентифицируют IP отправителя: при стороннем трафике "
        "на тестовом порту требуется изолировать VPS или дополнительная проверка источника.",
        "На VPS учитываются только входящие IPv4-пакеты с IP источника управляющей SSH-сессии "
        "Android. Если CGNAT использует иной внешний адрес для VPN-порта, доказательство "
        "останется отсутствующим, а не будет подменено проверкой чужих пакетов.",
        "Внутри нагрузочных ступеней измеряется реальный peak_inflight; меньше "
        "запрошенных потоков означает неполную нагрузку.",
        "Поля cumulative_mib показывают накопленную полезную нагрузку прогонки; "
        "один рост ошибок после порога НЕ доказывает срабатывание DPI без повторов.",
        "Использование Mihomo и на сервере, и на клиенте ограничивает переносимость результатов на иные реализации.",
        "Один тайм-аут или падение скорости не доказывает вмешательство ТСПУ. Для таких выводов нужны повторные испытания и контрольные сети.",
        "Байты учитываются для успешных HTTP-ответов; ошибочные подключения и заголовки дают дополнительный неучтённый трафик.",
    ])
    parallel_summary = "\n".join("| %s | %s |" %
                                 (label, str(max_verified_parallel[label])
                                  if max_verified_parallel[label] else "не подтверждено")
                                 for label in ORDER)
    summary = "\n".join("| %s | %d/%d | %.1f |" %
                        (label, successful[label], totals[label],
                         transferred[label] / (1024 * 1024))
                        for label in expected_protocols)
    details = "\n".join(reported)
    limits = "\n".join("- " + note for note in notes)
    expected_levels = len(expected_protocols) * len(meta.get("levels", []))
    throughput_complete = (bool(expected_levels) and len(good) == expected_levels
                           and controls_passed and not absent and not meta.get("error")
                           and bool(resources)
                           and all(resources_for(r, resources) is not None
                                   and resources_for(r, resources)["mihomo_present"] for r in good))
    complete = throughput_complete and len(good_rtt) == len(good)
    smoke_success = (mode == "smoke" and bool(expected_levels)
                     and not absent and not meta.get("error")
                     and controls_passed
                     and sum(r.get("status") in ("smoke_pass", "smoke_degraded")
                             for group in by_label.values() for r in group) == expected_levels
                     and all(int(num(r.get("route_packets"))) > 0 for label in expected_protocols
                             for r in by_label[label]))
    if mode == "smoke":
        status = ("Проверка доступности выполнена" if smoke_success
                  else "Проверка доступности неполная")
    elif complete:
        status = "Полный нагрузочный бенчмарк, включая p95"
    elif throughput_complete:
        status = "Нагрузочный бенчмарк: скорость подтверждена, p95 частично недоступен"
    else:
        status = "Пробный/неполный прогон"
    windows = read_csv(folder / "time-windows.csv")
    timeline_chart = ""
    if mode == "soak" and windows:
        timeline_points = [(str(w.get("protocol", "")) + " / " +
                            str(w.get("concurrency", "")) + " потоков / " +
                            str(w.get("window_start_sec", "?")) + " c",
                            num(w.get("throughput_mib_s")))
                           for w in windows]
        chart(charts / "time-windows.svg",
              "Наблюдаемая скорость по 10-секундным интервалам",
              timeline_points, "МиБ/с")
        timeline_chart = "![Динамика по интервалам](charts/time-windows.svg)"
    document = """# Автоматический отчёт о полевом тестировании AnyModel

**Статус:** {status}.

**Режим:** {mode}. Значения для smoke нельзя переносить на нагрузочный benchmark.

## Условия эксперимента

- Время начала (UTC): {date}
- Сеть: {network} (**метка введена вручную**)
- Сервер: {server}
- Клиент: Android/Termux, {client}
- Полезная нагрузка одного запроса: {size} байт
- Уровни параллельных HTTP-запросов: {levels}
- Плановое время одного уровня: {seconds} с (фактическое время для каждого уровня — в таблице)
- Верхняя граница отправленных ответов: {budget} МиБ (считая полный ответ даже при ошибке проверки)
- Успешных запросов за весь тест: {ok}/{count}
- Подтверждённый объём успешных HTTP-ответов: {mib:.2f} МиБ

Тестовый HTTP-сервис работал **только на loopback VPS**, запросы отправлялись с Android через выбранный
профиль Mihomo. Подключения не использовали USB-модем, общий VPN на ПК или публикацию origin наружу.
Между этапами ядро Mihomo на телефоне переключало маршрутизацию через локальный REST API.

## Проверенный уровень параллельности

Максимальная **наблюдавшаяся** параллельность, которая прошла весь интервал,
дала достаточные выборки HTTP ping/скачиваний, подтверждённый маршрут и CPU/RAM
(это НЕ абсолютный предел пропускной способности VPS):

| Протокол | Максимум подтверждённых одновременных HTTP-запросов |
|---|---:|
{parallel_summary}

## Сравнение протоколов

| Протокол | Успешные/все запросы | Подтверждённый трафик, МиБ |
|---|---:|---:|
{summary}

| Протокол | Потоки | OK/все | Факт/план, с | МиБ/с | HTTP ping p50 / p95, мс | Ping OK / ошибки | Загрузка p95, мс | Порт VPS (вх. пакеты) | Накоплено, МиБ | CPU Mihomo peak, % ядра | RSS peak, МиБ | Статус |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---|
{details}

100% CPU = одно занятое логическое ядро; это не процент всех ядер VPS.
В режиме smoke статусы `smoke_pass` / `smoke_degraded` подтверждают доступность
и не требуют p95. В режиме load/soak `ok` означает полный интервал,
подтверждённую параллельность, проверенные ответы, >=20 успешных загрузок,
минимальный процент успешности из параметров запуска (по умолчанию 95%), отрицательные контроли и подтверждение порта VPS.
p95 HTTP /ping рассчитывается **отдельно** только при >=20 успешных пробах.
При меньшей выборке это «—», но валидная скорость не исчезает. `budget_limited` означает завершение из-за лимита до таймера; `insufficient_samples` — слишком мало успешных ответов. Строки с этими статусами полезны как функциональная проверка, но НЕ сопоставимы как полноценный нагрузочный тест. p95 доступен только при >=20 успешных ответах, p99 — при >=100. Недоступные метрики указаны «—», а не нулём.

![Скорость (только подтверждённые полные интервалы)](charts/throughput.svg)

![HTTP ping p95 (только при достаточном числе проб)](charts/latency.svg)

{timeline_chart}

Исходные временные окна (каждые 10 секунд): time-windows.csv.
Скорость в окне привязана к моменту завершения запроса, а не к точному времени
передачи каждого байта. Одно падение в окне не доказывает ТСПУ.

CSV с первичными данными: measurements.csv, request-samples.csv, server-resources.csv.
Файл negative-controls.json подтверждает, что DIRECT и заведомо неисправный SOCKS5
не смогли получить удалённый origin. Для каждого уровня сохранены порт, число
реально входящих IPv4-пакетов **от того же внешнего IPv4, что управляющая SSH-сессия Android**,
и их общий размер на VPS, измеренный независимым
AF_PACKET-сборщиком (без полезной нагрузки и IP клиентов). Наличие входящих
пакетов соответствующего порта и SHA256+nonce подтверждает маршрут гораздо сильнее,
чем один лишь выбор профиля в API. При одновременном стороннем трафике на другие
тестовые порты выставляется route_ambiguous: без изоляции VPS это нестрогое доказательство.
Сырые записи каждого запроса — request-samples.csv; туда НЕ записаны пароли и URL подписок.
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
        status=status, mode=mode, timeline_chart=timeline_chart,
        date=date, network=network, server=server,
        client=meta.get("client_version", "Mihomo")[:100],
        size=meta.get("payload_bytes", 0), levels=meta.get("levels", []),
        seconds=meta.get("seconds_per_level", "—"),
        budget=meta.get("max_mib", "—"), ok=all_ok, count=all_requests,
        mib=total_bytes / (1024 * 1024), summary=summary,
        parallel_summary=parallel_summary,
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
