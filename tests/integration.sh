#!/usr/bin/env bash
# Интеграционные тесты на временном GitHub Actions runner, без VPS и Let's Encrypt.
set -Eeuo pipefail
# Вспомогательная функция: перенаправляет вывод в журнал пользователя runner, а не root.
run_root() { sudo "$@"; }
umask 077
cd "$(dirname "$0")/.."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

log() { printf '\n[ИНТЕГРАЦИОННЫЙ ТЕСТ] %s\n' "$*"; }
assert_file() { sudo test -s "$1" || { echo "Нет файла или доступ ограничен: $1" >&2; exit 1; }; }
assert_eq() { [[ "$1" == "$2" ]] || { echo "Ожидалось '$2', получено '$1'" >&2; exit 1; }; }

# Тестовый CA используется только внутри одноразового runner.
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 1 \
  -subj '/CN=test-tspu-ci-ca' \
  -keyout "$TMP/ca.key" -out "$TMP/ca.pem" >/dev/null 2>&1

make_cert() {
  local name="$1" san="$2"
  openssl req -new -newkey rsa:2048 -nodes -sha256 \
    -subj "/CN=$name" -keyout "$TMP/$name.key" -out "$TMP/$name.csr" >/dev/null 2>&1
  cat > "$TMP/$name.ext" <<EOF
subjectAltName=$san
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF
  openssl x509 -req -days 1 -sha256 -in "$TMP/$name.csr" \
    -CA "$TMP/ca.pem" -CAkey "$TMP/ca.key" -CAcreateserial \
    -out "$TMP/$name.pem" -extfile "$TMP/$name.ext" >/dev/null 2>&1
}
make_cert ip-a "IP:8.8.8.8"
make_cert ip-b "IP:1.1.1.1"
make_cert panel "DNS:panel.example.test"
make_cert duck "DNS:example.duckdns.org"

# Тестовый CA делаем доверенным ОС, чтобы повторный запуск проверял HTTPS без особых флагов.
sudo install -m 0644 "$TMP/ca.pem" /usr/local/share/ca-certificates/test-tspu-ci.crt
sudo update-ca-certificates >/dev/null

check_https() {
  local host="$1" code path
  # m-ui печатает установленный путь админки в journal при запуске.
  path="$(sudo journalctl -u m-ui -o cat --no-pager |
    grep -oE 'https://0[.]0[.]0[.]0:2053/[[:alnum:]]+/' |
    tail -n 1 | sed 's#https://0.0.0.0:2053##')"
  [[ -n "$path" ]] || { echo "Не найден реальный путь панели в journalctl" >&2; exit 1; }
  code="$(curl -sS -L --max-redirs 3 --noproxy '*' --cacert "$TMP/ca.pem" \
    --connect-timeout 3 --max-time 8 \
    --resolve "$host:2053:127.0.0.1" \
    -o "$TMP/panel.html" -w '%{http_code}' "https://$host:2053$path")"
  [[ "$code" == 200 ]] || { echo "Страница панели вернула HTTP $code" >&2; exit 1; }
  [[ -s "$TMP/panel.html" ]] || { echo "Пустая страница входа" >&2; exit 1; }
  log "GUI $host:2053$path возвращает HTTP 200, TLS-сертификат доверенный"
}

log "Запуск настоящего деплоя: Mihomo, m-ui, systemd и TLS"
run_root bash deploy.sh --ip 8.8.8.8 --cert-file "$TMP/ip-a.pem" \
  --key-file "$TMP/ip-a.key" --ca-cert "$TMP/ca.pem" >"$TMP/install.log" 2>&1 || {
    cat "$TMP/install.log"; sudo journalctl -u m-ui -n 50 --no-pager || true; exit 1;
  }

assert_file /usr/local/m-ui/mihomo
assert_file /usr/local/m-ui/m-ui
assert_file /usr/local/m-ui/data/state.json
assert_file /etc/sysctl.d/90-test-tspu.conf
sudo systemctl is-active --quiet m-ui.service
sudo systemctl is-enabled --quiet m-ui.service
sudo systemctl is-enabled --quiet test-tspu-ip-check.timer
sudo /usr/local/m-ui/mihomo -v >/dev/null
sudo /usr/local/m-ui/m-ui version >/dev/null
assert_eq "$(sudo cat /var/lib/test-tspu/panel-host)" "8.8.8.8"
assert_eq "$(sudo cat /var/lib/test-tspu/cert-mode)" "custom"
check_https 8.8.8.8

log "Проверка создания inbounds в панели и прослушивания портов"
sudo python3 - <<'PY'
import json
p = '/usr/local/m-ui/data/state.json'
with open(p) as f: state = json.load(f)
names = {x['name'] for x in state['inbounds']}
required = {'test-tspu-ss', 'test-tspu-hysteria2', 'test-tspu-tuic',
            'test-tspu-trojan', 'test-tspu-anytls', 'test-tspu-vless-xhttp'}
assert required <= names, names
PY
sudo stat -c %a /var/lib/test-tspu/panel-password | grep -qx 600
sudo stat -c %a /var/lib/test-tspu/inbound-secrets.json | grep -qx 600
for port in 8388 9443 10443 11443; do
  found=0
  for _ in {1..25}; do
    if sudo ss -ltn | grep -Eq ":${port}[[:space:]]"; then found=1; break; fi
    sleep 0.2
  done
  if ((!found)); then
    echo "TCP $port не слушает после ожидания" >&2
    sudo ss -lntup
    sudo grep -E '^[[:space:]]*(name|type|port):' /usr/local/m-ui/data/config.yaml | tail -90 || true
    # m-ui сохраняет логи ядра в памяти. Показываем только diagnostic строки.
    panel_path="$(sudo python3 -c 'import json; print("/"+json.load(open("/usr/local/m-ui/data/state.json"))["settings"]["panelPath"].strip("/")+"/")')"
    sudo curl -sS --noproxy '*' --cacert "$TMP/ca.pem" \
      --resolve '8.8.8.8:2053:127.0.0.1' \
      --cookie /var/lib/test-tspu/api-session.cookies \
      "https://8.8.8.8:2053${panel_path}api/core/logs" |
      python3 -c 'import json,sys; x=json.load(sys.stdin); print(str(x.get("data"))[-2400:])' || true
    exit 1
  fi
done
for port in 14443 8443; do
  sudo ss -lun | grep -Eq ":${port}[[:space:]]" || {
    echo "UDP $port не слушает" >&2; exit 1;
  }
done

log "Сквозные тесты: Android/Mihomo-подписка, TLS и HTTP через каждый inbound"
bash tests/proxy-e2e.sh 8.8.8.8 "$TMP/ca.pem"

log "Проверка секретов и локального API"
sudo stat -c %a /var/lib/test-tspu/subscriptions.txt | grep -qx 600
sudo stat -c %a /var/lib/test-tspu/panel-user | grep -qx 600
sudo stat -c %a /var/lib/test-tspu/panel-password | grep -qx 600
sudo ss -lnt | grep -Eq '127[.]0[.]0[.]1:9093[[:space:]]'
if sudo ss -lnt | grep -Eq '(^|[[:space:]])(0[.]0[.]0[.]0|\*):9093[[:space:]]'; then
  echo 'API Mihomo доступен публично!' >&2
  exit 1
fi
if grep -Eq '^(Username|Password):' "$TMP/install.log"; then
  echo 'Пароли m-ui попали в лог инсталлятора' >&2
  exit 1
fi
panel_uri="$(sudo /usr/bin/python3 - <<'PY'
import json
data=json.load(open('/usr/local/m-ui/data/state.json'))
print('/'+data['settings']['panelPath'].strip('/')+'/api/inbounds')
PY
)"
api_code="$(curl --silent --show-error --noproxy '*' \
  --cacert "$TMP/ca.pem" --resolve '8.8.8.8:2053:127.0.0.1' \
  --max-time 6 -o /dev/null -w '%{http_code}' "https://8.8.8.8:2053$panel_uri")"
assert_eq "$api_code" 401

log "Повторный деплой: отсутствие изменений и прежний PID"
pid_before="$(sudo systemctl show -p MainPID --value m-ui.service)"
subscriptions_before="$(sudo sha256sum /var/lib/test-tspu/subscriptions.txt | awk '{print $1}')"
sysctl_before="$(sudo stat -c %Y /etc/sysctl.d/90-test-tspu.conf)"
unit_before="$(sudo stat -c %Y /etc/systemd/system/m-ui.service.d/90-test-tspu.conf)"
run_root bash deploy.sh --ip 8.8.8.8 >"$TMP/repeat.log" 2>&1 || { cat "$TMP/repeat.log"; exit 1; }
assert_eq "$(sudo cat /var/lib/test-tspu/panel-host)" "8.8.8.8"
sudo systemctl is-active --quiet m-ui.service
assert_eq "$(sudo systemctl show -p MainPID --value m-ui.service)" "$pid_before"
assert_eq "$(sudo sha256sum /var/lib/test-tspu/subscriptions.txt | awk '{print $1}')" "$subscriptions_before"
assert_eq "$(sudo stat -c %Y /etc/sysctl.d/90-test-tspu.conf)" "$sysctl_before"
assert_eq "$(sudo stat -c %Y /etc/systemd/system/m-ui.service.d/90-test-tspu.conf)" "$unit_before"
grep -q 'Итог: без изменений' "$TMP/repeat.log"
check_https 8.8.8.8

log "Восстановление удалённой конфигурации без переустановки"
sudo rm /etc/systemd/system/m-ui.service.d/90-test-tspu.conf
run_root bash deploy.sh --ip 8.8.8.8 >"$TMP/reconcile.log" 2>&1 || { cat "$TMP/reconcile.log"; exit 1; }
assert_file /etc/systemd/system/m-ui.service.d/90-test-tspu.conf
grep -q 'Изменено: /etc/systemd/system/m-ui.service.d/90-test-tspu.conf' "$TMP/reconcile.log"
check_https 8.8.8.8

log "Восстановление повреждённого бинарника ядра (без смены настроек панели)"
sudo install -m 0755 /usr/bin/true /usr/local/m-ui/mihomo.stale
sudo mv -f /usr/local/m-ui/mihomo.stale /usr/local/m-ui/mihomo
run_root bash deploy.sh --ip 8.8.8.8 >"$TMP/repair-core.log" 2>&1 || { cat "$TMP/repair-core.log"; exit 1; }
sudo /usr/local/m-ui/mihomo -v | grep -Fq 'v1.19.34'
grep -q 'Изменено: Mihomo' "$TMP/repair-core.log"
check_https 8.8.8.8

log "Восстановление устаревшей версии панели с сохранением аккаунта и HTTPS"
sudo install -m 0755 /usr/bin/true /usr/local/m-ui/m-ui.stale
sudo mv -f /usr/local/m-ui/m-ui.stale /usr/local/m-ui/m-ui
run_root bash deploy.sh --ip 8.8.8.8 >"$TMP/repair-panel.log" 2>&1 || { cat "$TMP/repair-panel.log"; exit 1; }
sudo /usr/local/m-ui/m-ui version | grep -Fq 'v0.3.3'
grep -q 'Изменено: m-ui' "$TMP/repair-panel.log"
check_https 8.8.8.8

log "После обновлений повторный запуск опять ничего не меняет"
pid_before="$(sudo systemctl show -p MainPID --value m-ui.service)"
run_root bash deploy.sh --ip 8.8.8.8 >"$TMP/post-upgrade-noop.log" 2>&1 || { cat "$TMP/post-upgrade-noop.log"; exit 1; }
assert_eq "$(sudo systemctl show -p MainPID --value m-ui.service)" "$pid_before"
grep -q 'Итог: без изменений' "$TMP/post-upgrade-noop.log"

log "Смена IP с новым сертификатом"
run_root bash deploy.sh --ip 1.1.1.1 --cert-file "$TMP/ip-b.pem" \
  --key-file "$TMP/ip-b.key" --ca-cert "$TMP/ca.pem" >"$TMP/change-ip.log" 2>&1 || {
    cat "$TMP/change-ip.log"; exit 1;
  }
assert_eq "$(sudo cat /var/lib/test-tspu/panel-host)" "1.1.1.1"
check_https 1.1.1.1

log "Проверка --sync-ip без изменения адреса"
run_root bash deploy.sh --sync-ip --ip 1.1.1.1 >"$TMP/nochange.log"
grep -q 'IP не изменился' "$TMP/nochange.log"

log "Имитация перевыпуска ACME: успех и ошибка с сохранением старого IP"
sudo cp /var/lib/test-tspu/m-ui-upstream-install.sh "$TMP/original-upstream.sh"
sudo tee /var/lib/test-tspu/m-ui-upstream-install.sh >/dev/null <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == --ssl-menu && "$2" == --ssl-ip && "$3" == 9.9.9.9 ]] || exit 7
mkdir -p /root/cert/ip
printf 'симулированный сертификат\n' > /root/cert/ip/fullchain.pem
MOCK
run_root bash deploy.sh --sync-ip --ip 9.9.9.9 >"$TMP/change-auto.log"
assert_eq "$(sudo cat /var/lib/test-tspu/panel-host)" "9.9.9.9"
sudo grep -q 'https://9.9.9.9:2053/' /var/lib/test-tspu/subscriptions.txt

sudo tee /var/lib/test-tspu/m-ui-upstream-install.sh >/dev/null <<'MOCK'
#!/usr/bin/env bash
exit 8
MOCK
if run_root bash deploy.sh --sync-ip --ip 4.4.4.4 >"$TMP/fail-auto.log" 2>&1; then
  echo "Ошибочный перевыпуск неожиданно прошёл" >&2; exit 1
fi
assert_eq "$(sudo cat /var/lib/test-tspu/panel-host)" "9.9.9.9"
sudo cp "$TMP/original-upstream.sh" /var/lib/test-tspu/m-ui-upstream-install.sh

log "Переключение на домен с локальным тестовым сертификатом"
run_root bash deploy.sh --domain panel.example.test --cert-file "$TMP/panel.pem" \
  --key-file "$TMP/panel.key" --ca-cert "$TMP/ca.pem" >"$TMP/domain.log" 2>&1 || {
    cat "$TMP/domain.log"; exit 1;
  }
assert_eq "$(sudo cat /var/lib/test-tspu/panel-host)" "panel.example.test"
assert_eq "$(sudo cat /var/lib/test-tspu/panel-mode)" "domain"
check_https panel.example.test
run_root bash deploy.sh --sync-ip --ip 8.8.8.8 >"$TMP/domain-sync.log"
grep -q 'Для домена сертификат привязан к имени' "$TMP/domain-sync.log"

log "DuckDNS: запись, смена IP, отсутствие повторных запросов и хранение токена"
mkdir -p "$TMP/bin"
cat >"$TMP/bin/curl" <<'MOCK'
#!/usr/bin/env bash
for arg in "$@"; do
  if [[ "$arg" == 'https://www.duckdns.org/update' ]]; then
    printf 'OK\n'
    printf 'updated\n' >> "$MOCK_LOG"
    exit 0
  fi
done
exec /usr/bin/curl "$@"
MOCK
chmod 755 "$TMP/bin/curl"
printf 'test-duckdns-token-%s\n' "$RANDOM" >"$TMP/duckdns-token"
run_root env PATH="$TMP/bin:$PATH" MOCK_LOG="$TMP/duckdns.log" bash deploy.sh \
  --duckdns example.duckdns.org --duckdns-token-file "$TMP/duckdns-token" --ip 8.8.8.8 \
  --cert-file "$TMP/duck.pem" --key-file "$TMP/duck.key" --ca-cert "$TMP/ca.pem" \
  >"$TMP/duckdns-install.log" 2>&1 || { cat "$TMP/duckdns-install.log"; exit 1; }
assert_eq "$(sudo cat /var/lib/test-tspu/panel-host)" "example.duckdns.org"
assert_eq "$(sudo cat /var/lib/test-tspu/duckdns-current-ip)" "8.8.8.8"
assert_eq "$(sudo stat -c %a /var/lib/test-tspu/duckdns-token)" "600"
check_https example.duckdns.org
run_root env PATH="$TMP/bin:$PATH" MOCK_LOG="$TMP/duckdns.log" bash deploy.sh \
  --sync-ip --ip 1.1.1.1 >"$TMP/duckdns-sync.log" 2>&1 || {
    cat "$TMP/duckdns-sync.log"; exit 1;
  }
assert_eq "$(sudo cat /var/lib/test-tspu/duckdns-current-ip)" "1.1.1.1"
sudo grep -q 'https://example.duckdns.org:2053/' /var/lib/test-tspu/subscriptions.txt
run_root env PATH="$TMP/bin:$PATH" MOCK_LOG="$TMP/duckdns.log" bash deploy.sh \
  --sync-ip --ip 1.1.1.1 >"$TMP/duckdns-noop.log"
assert_eq "$(sudo cat "$TMP/duckdns.log" | wc -l)" "2"

log "Проверка отката сетевых настроек"
run_root bash deploy.sh --rollback-tuning >"$TMP/rollback.log"
[[ ! -e /etc/sysctl.d/90-test-tspu.conf ]]
[[ ! -e /etc/systemd/system/m-ui.service.d/90-test-tspu.conf ]]

log "Проверка ранней валидации аргументов"
if bash deploy.sh --unknown >"$TMP/bad.log" 2>&1; then exit 1; fi
if bash deploy.sh --cert-file "$TMP/ip-a.pem" >"$TMP/bad.log" 2>&1; then exit 1; fi
if bash deploy.sh --domain a.example --ip 1.1.1.1 >"$TMP/bad.log" 2>&1; then exit 1; fi

log "УСПЕХ: реальный деплой и HTTPS, смена IP/домена, имитация обновления, откат"
