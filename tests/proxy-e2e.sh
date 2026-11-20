#!/usr/bin/env bash
# Настоящий клиент Mihomo подключается к запущенным inbound того же runner.
# Публичный VPS не требуется; TLS проверяется через тестовый CA.
set -Eeuo pipefail
umask 077

HOST="${1:?укажите адрес тестового сервера}"
CA="${2:?укажите тестовый CA}"
INSTANCE="${3:-1}"
[[ "$INSTANCE" =~ ^[12]$ ]] || { echo "E2E: нужен номер прогона 1 или 2" >&2; exit 1; }
MIXED_PORT=$((17890 + INSTANCE))
CTRL_PORT=$((19090 + INSTANCE))
export MIXED_PORT CTRL_PORT
ROOT="$(mktemp -d)"
CHILD_PID=''
ORIGIN_PID=''
cleanup() {
  [[ -z "$CHILD_PID" ]] || kill "$CHILD_PID" >/dev/null 2>&1 || true
  # sudo может оставить дочерний процесс Mihomo после завершения оболочки.
  # Выбираем строго процесс с временной директорией ЭТОГО теста.
  sudo pgrep -f "^/usr/local/m-ui/mihomo -d ${ROOT}/client-home " 2>/dev/null |
    xargs -r sudo kill -TERM 2>/dev/null || true
  [[ -z "$ORIGIN_PID" ]] || kill "$ORIGIN_PID" >/dev/null 2>&1 || true
  rm -rf "$ROOT"
}
trap cleanup EXIT

fatal() {
  echo "E2E: $*" >&2
  for logfile in "$ROOT/validate.log" "$ROOT/client.log"; do
    if [[ -s "$logfile" ]]; then
      echo 'Ошибки конфигурации/ядра:' >&2
      grep -Ei 'error|warn|fail|panic|invalid' "$logfile" | tail -18 >&2 || true
    fi
  done
  if [[ -s "$ROOT/validate.log" ]]; then
    # Прячем возможные значения секретов в тексте ошибки.
    sed -E 's/(password|uuid|private-key|token|secret):[^[:space:]]+/\1:[СКРЫТО]/Ig' \
      "$ROOT/validate.log" | tail -20 >&2
  fi
  exit 1
}

# Секретный токен не выводим и не сохраняем в артефактах GitHub.
URL="$(sudo /usr/bin/python3 - <<'PY'
from pathlib import Path
f=Path('/var/lib/test-tspu/subscriptions.txt').read_text()
section = None
for line in f.splitlines():
    if line.startswith('# Подписка: '):
        section = line.removeprefix('# Подписка: ')
    elif section == 'test' and line.startswith('Mihomo/Clash: '):
        print(line.removeprefix('Mihomo/Clash: '))
        break
else:
    raise SystemExit('Не создана подписка пользователя test в m-ui')
PY
)"
[[ "$URL" == "https://$HOST:2053/"* ]] || fatal 'некорректный адрес подписки'
sudo stat -c %a /var/lib/test-tspu/subscriptions.txt | grep -qx 600 ||
  fatal 'файл ссылок подписок должен быть 0600'

curl --noproxy '*' --silent --show-error --fail \
  --cacert "$CA" --resolve "$HOST:2053:127.0.0.1" \
  --max-time 10 "$URL" -o "$ROOT/subscription.yaml" ||
    fatal 'не удалось получить подписку по проверенному HTTPS'
grep -q 'proxies:' "$ROOT/subscription.yaml" || fatal 'подписка не содержит прокси'

# Клиентские форматы Android и страница с QR действительно доступны.
BASE="${URL%/clash}"
for FORMAT in v2rayng happ; do
  curl --noproxy '*' --silent --show-error --fail \
    --cacert "$CA" --resolve "$HOST:2053:127.0.0.1" \
    --max-time 10 "$BASE/$FORMAT" -o "$ROOT/$FORMAT.b64" ||
      fatal "недоступна Android-подписка $FORMAT"
  base64 -d "$ROOT/$FORMAT.b64" > "$ROOT/$FORMAT.links" ||
    fatal "Android-подписка $FORMAT не является Base64"
  grep -Eq '^(vless|trojan|hysteria2|tuic|ss)://' "$ROOT/$FORMAT.links" ||
    fatal "Android-подписка $FORMAT не содержит ссылок"
  echo "E2E OK: Android-подписка $FORMAT"
done
curl --noproxy '*' --silent --show-error --fail \
  --cacert "$CA" --resolve "$HOST:2053:127.0.0.1" \
  --header 'Accept: text/html' --max-time 10 "$BASE" -o "$ROOT/qr.html" ||
    fatal 'недоступна HTML-страница с QR'
grep -Eiq 'qr|qrious' "$ROOT/qr.html" || fatal 'страница подписки не содержит QR-интерфейс'
echo 'E2E OK: HTML-страница подписки с QR'

# Проверка отсутствия доступа без правильного токена.
bad_url="https://$HOST:2053/invalid-tspu-subscription-token/clash"
code="$(curl --noproxy '*' --silent --show-error --cacert "$CA" \
  --resolve "$HOST:2053:127.0.0.1" \
  --max-time 6 -o /dev/null -w '%{http_code}' "$bad_url")"
[[ "$code" == 404 ]] || fatal "чужой токен подписки возвращает HTTP $code вместо 404"

# PyYAML ставится только в тестовое окружение, не на пользовательский VPS.
# Адрес клиентских прокси заменяем на loopback, но оставляем TLS SNI
# исходного адреса, чтобы строго проверить сертификат.
HOST="$HOST" ROOT="$ROOT" /usr/bin/python3 - <<'PY'
import json, os, pathlib, yaml
host = os.environ['HOST']
root = pathlib.Path(os.environ['ROOT'])
config = yaml.safe_load((root/'subscription.yaml').read_text())
proxies = config.get('proxies')
if not isinstance(proxies,list) or len(proxies)<6:
    raise SystemExit('Ожидалось не менее 6 типов клиентов в подписке m-ui')
required = {8388:'ss', 14443:'hysteria2', 8443:'tuic',
            9443:'trojan', 10443:'anytls', 11443:'vless'}
present = {int(x['port']):x['type'] for x in proxies}
for port, kind in required.items():
    if present.get(port) != kind:
        raise SystemExit(f'Не найден профиль {kind}:{port} в подписке')
expected=set(required) | ({443} if present.get(443)=='vless' else set())
if any(x.get('skip-cert-verify') is True for x in proxies):
    raise SystemExit('Клиентская подписка небезопасно отключает проверку TLS')
for x in proxies:
    if x.get('server') != host:
        raise SystemExit(f'Неожиданный адрес сервера в подписке: {x.get("name")}')
    x['server']='127.0.0.1'
    if 'reality-opts' not in x:
        if x.get('type') in {'hysteria2','tuic','anytls'}:
            x['sni']=host
        if x.get('type') == 'trojan':
            x['sni']=host
        if x.get('type') == 'vless':
            x['servername']=host
    # REALITY оставляет внешний проверенный SNI, UUID и public key без изменений.
config['mixed-port']=int(os.environ['MIXED_PORT'])
config['allow-lan']=False
config['mode']='rule'
config['external-controller']='127.0.0.1:'+os.environ['CTRL_PORT']
config['secret']='ci-local-only'
config['log-level']='warning'
config['proxy-groups']=[{'name':'E2E', 'type':'select',
                         'proxies':[proxy['name'] for proxy in proxies]}]
config['rules']=['MATCH,E2E']
(root/'client.yaml').write_text(yaml.safe_dump(config,sort_keys=False,allow_unicode=True))
(root/'targets.json').write_text(json.dumps([
    {'port':int(x['port']),'type':x['type'],'name':x['name']}
    for x in proxies if int(x['port']) in expected
]))
PY

mkdir -p "$ROOT/client-home" "$ROOT/origin"
printf 'tspu-e2e-through-mihomo\n' > "$ROOT/origin/probe"
python3 -m http.server 18180 --bind 127.0.0.1 --directory "$ROOT/origin" \
  > "$ROOT/origin.log" 2>&1 &
ORIGIN_PID=$!

# shellcheck disable=SC2024
sudo /usr/local/m-ui/mihomo -d "$ROOT/client-home" -t -f "$ROOT/client.yaml" \
  > "$ROOT/validate.log" 2>&1 ||
    fatal 'невалидная клиентская конфигурация Mihomo'

# shellcheck disable=SC2024
sudo /usr/local/m-ui/mihomo -d "$ROOT/client-home" -f "$ROOT/client.yaml" \
  > "$ROOT/client.log" 2>&1 &
CHILD_PID=$!
ready=0
for _ in {1..35}; do
  if curl --silent --fail --noproxy '*' --max-time 1 \
    -H 'Authorization: Bearer ci-local-only' \
    "http://127.0.0.1:${CTRL_PORT}/proxies/E2E" >/dev/null; then
    ready=1
    break
  fi
  sleep 0.2
done
((ready)) || fatal 'клиент Mihomo не запустился'

# Обходим не просто проверку TCP: запрос /probe обслуживает локальный HTTP
# сервер, доступ к которому происходит ЧЕРЕЗ выбранный удалённый inbound.
while IFS=$'\t' read -r port kind name; do
  [[ -n "$name" ]] || continue
  payload="$(NAME="$name" python3 - <<'PY'
import json,os
print(json.dumps({'name':os.environ['NAME']}))
PY
)"
  curl --noproxy '*' --silent --show-error --fail --max-time 4 \
    --request PUT --header 'Authorization: Bearer ci-local-only' \
    --header 'Content-Type: application/json' \
    --data "$payload" "http://127.0.0.1:${CTRL_PORT}/proxies/E2E" >/dev/null ||
      fatal "не получилось выбрать $kind/$port"
  ok=0
  for _ in {1..2}; do
    if curl --noproxy '' --silent --show-error --fail \
      --connect-timeout 3 --max-time 8 \
      --proxy "http://127.0.0.1:${MIXED_PORT}" \
      'http://127.0.0.1:18180/probe' 2>/dev/null |
      grep -Fxq 'tspu-e2e-through-mihomo'; then
      ok=1
      break
    fi
  done
  ((ok)) || fatal "HTTP-трафик через $kind/$port не прошёл"
  echo "E2E OK: $kind/$port — HTTP-запрос через прокси получен"
done < <(/usr/bin/python3 - "$ROOT/targets.json" <<'PY'
import json,sys
for item in json.load(open(sys.argv[1])):
    print(str(item['port'])+'\t'+item['type']+'\t'+item['name'])
PY
)
# Дымовой бенчмарк на одном runner: он проверяет КОД замеров,
# но его производительность нельзя переносить на российские сети.
head -c 131072 /dev/urandom > "$ROOT/origin/payload.bin"
python3 bench/server_monitor.py --duration 2 --interval 0.5 \
  --output "$ROOT/server-monitor.csv" > "$ROOT/monitor.log" 2>&1 &
MONITOR_PID=$!
python3 bench/http_benchmark.py \
  --url 'http://127.0.0.1:18180/payload.bin' \
  --proxy "http://127.0.0.1:${MIXED_PORT}" \
  --levels 1,8 --seconds 0.6 --timeout 5 --min-bytes 131072 \
  --label CI-loopback --output "$ROOT/benchmark.csv" ||
  fatal 'бенчмарк не смог передать нагрузку через Mihomo'
wait "$MONITOR_PID" || fatal 'монитор CPU/RAM завершился с ошибкой'
python3 - "$ROOT/benchmark.csv" "$ROOT/server-monitor.csv" <<'PY'
import csv, sys
with open(sys.argv[1]) as file:
    rows = list(csv.DictReader(file))
assert len(rows) == 2
assert all(int(row['requests_ok']) > 0 for row in rows)
with open(sys.argv[2]) as file:
    rows = list(csv.DictReader(file))
assert len(rows) >= 2
assert any(int(row['mihomo_count']) > 0 for row in rows)
print('E2E OK: бенчмарк throughput/latency и монитор CPU/RSS')
PY
echo 'E2E OK: подписки, TLS, API и реальная передача данных'
