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
  code="$(curl -sS --noproxy '*' --cacert "$TMP/ca.pem" \
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

log "Повторный деплой не должен сбрасывать GUI или запрашивать ACME"
run_root bash deploy.sh --ip 8.8.8.8 >"$TMP/repeat.log" 2>&1 || { cat "$TMP/repeat.log"; exit 1; }
assert_eq "$(sudo cat /var/lib/test-tspu/panel-host)" "8.8.8.8"
sudo systemctl is-active --quiet m-ui.service
check_https 8.8.8.8

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

log "Проверка отката сетевых настроек"
run_root bash deploy.sh --rollback-tuning >"$TMP/rollback.log"
[[ ! -e /etc/sysctl.d/90-test-tspu.conf ]]
[[ ! -e /etc/systemd/system/m-ui.service.d/90-test-tspu.conf ]]

log "Проверка ранней валидации аргументов"
if bash deploy.sh --unknown >"$TMP/bad.log" 2>&1; then exit 1; fi
if bash deploy.sh --cert-file "$TMP/ip-a.pem" >"$TMP/bad.log" 2>&1; then exit 1; fi
if bash deploy.sh --domain a.example --ip 1.1.1.1 >"$TMP/bad.log" 2>&1; then exit 1; fi

log "УСПЕХ: реальный деплой и HTTPS, смена IP/домена, имитация обновления, откат"
