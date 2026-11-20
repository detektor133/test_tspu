#!/usr/bin/env bash
# Установка Mihomo и m-ui на Ubuntu, без Docker. Сетевые настройки меняются умеренно.
set -Eeuo pipefail
umask 077

PANEL_VERSION='v0.3.3'
CORE_VERSION='v1.19.34'
PANEL_PORT=2053
DATA_DIR='/var/lib/test-tspu'
TUNING_FILE='/etc/sysctl.d/90-test-tspu.conf'
DROPIN_DIR='/etc/systemd/system/m-ui.service.d'
INSTALLER_URL='https://raw.githubusercontent.com/RomanovCaesar/m-ui/v0.3.3/install.sh'
FIREWALL=0
SSH_PORT=''
MODE='install'
PUBLIC_PANEL_HOST=''

fail() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
log() { printf '[test-tspu] %s\n' "$*"; }

usage() {
  cat <<'HELP'
Запуск: sudo bash deploy.sh [--public-panel ДОМЕН_ИЛИ_IP] [--firewall --ssh-port ПОРТ] [--check | --rollback-tuning]

Без параметров: установить Mihomo + m-ui на Ubuntu и включить умеренные сетевые настройки.
GUI по умолчанию доступен только локально (127.0.0.1:2053), через SSH-туннель.
--public-panel ДОМЕН_ИЛИ_IP : открыть GUI в интернете по HTTPS (на новом сервере).
    Нужен доступный домен или публичный IPv4 и возможность выпустить TLS-сертификат.
--firewall               : включить UFW только с явным --ssh-port.
--ssh-port ПОРТ          : действующий порт SSH на сервере.
--check                 : показать состояние, ничего не менять.
--rollback-tuning       : откатить только сетевые настройки и лимиты.
HELP
}
while (($#)); do
  case "$1" in
    --public-panel)
      (($# >= 2)) || fail 'нужен домен или IP после --public-panel'
      PUBLIC_PANEL_HOST="$2"; shift 2 ;;
    --firewall) FIREWALL=1; shift ;;
    --ssh-port) (($# >= 2)) || fail 'нужен номер порта после --ssh-port'; SSH_PORT="$2"; shift 2 ;;
    --check) MODE='check'; shift ;;
    --rollback-tuning) MODE='rollback'; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "неизвестный параметр: $1" ;;
  esac
done

require_root() { ((EUID == 0)) || fail 'запустите от root: sudo bash deploy.sh'; }

status() {
  log "Ядро Linux: $(uname -r)"
  log "congestion-control: $(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo n/a)"
  log "Дисциплина очереди: $(sysctl -n net.core.default_qdisc 2>/dev/null || echo n/a)"
  log "somaxconn: $(sysctl -n net.core.somaxconn 2>/dev/null || echo n/a)"
  log "Максимальный UDP-буфер приёма: $(sysctl -n net.core.rmem_max 2>/dev/null || echo n/a)"
  log "Максимальный UDP-буфер отправки: $(sysctl -n net.core.wmem_max 2>/dev/null || echo n/a)"
  if command -v systemctl >/dev/null 2>&1; then
    log "Сервис m-ui: $(systemctl is-active m-ui.service 2>/dev/null || true)"
  fi
  if [[ -x /usr/local/m-ui/mihomo ]]; then
    /usr/local/m-ui/mihomo -v | head -n 1
  fi
  if [[ -x /usr/local/m-ui/m-ui ]]; then
    /usr/local/m-ui/m-ui version | head -n 1
  fi
}

rollback() {
  require_root
  if [[ -f "$TUNING_FILE" ]]; then
    if [[ -f "${DATA_DIR}/previous-sysctl.conf" ]]; then
      cp -a "${DATA_DIR}/previous-sysctl.conf" "$TUNING_FILE"
    elif grep -q '^# managed by test-tspu deploy.sh$' "$TUNING_FILE"; then
      rm -f "$TUNING_FILE"
    else
      fail "не буду удалять изменённый файл $TUNING_FILE"
    fi
    if [[ -f "${DATA_DIR}/sysctl-before.values" ]]; then
      while IFS='=' read -r key value; do
        [[ -n "$key" && -n "$value" ]] || continue
        sysctl -w "$key=$value" >/dev/null || log "ПРЕДУПРЕЖДЕНИЕ: не удалось восстановить $key"
      done < "${DATA_DIR}/sysctl-before.values"
    fi
  fi
  if [[ -f "${DROPIN_DIR}/90-test-tspu.conf" ]]; then
    rm -f "${DROPIN_DIR}/90-test-tspu.conf"
    systemctl daemon-reload
    systemctl try-restart m-ui.service || true
  fi
  log 'Сетевые настройки откатили; Mihomo и GUI не удалены.'
}

if [[ "$MODE" == check ]]; then status; exit 0; fi
if [[ "$MODE" == rollback ]]; then rollback; exit 0; fi
require_root

[[ -r /etc/os-release ]] || fail 'не удалось определить ОС'
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || fail 'поддерживается только Ubuntu 22.04/24.04 LTS'
[[ "${VERSION_ID:-}" == '22.04' || "${VERSION_ID:-}" == '24.04' ]] || fail 'поддерживается только Ubuntu 22.04/24.04 LTS'
command -v systemctl >/dev/null 2>&1 || fail 'нужен systemd'
case "$(uname -m)" in
  x86_64) ARCH='amd64' ;;
  aarch64) ARCH='arm64' ;;
  *) fail 'нужен процессор amd64 или arm64' ;;
esac

# Публичная панель открывается только с TLS, по явному запросу.
TLS_ARGS=(--no-ssl)
PANEL_BIND='127.0.0.1'
if [[ -n "$PUBLIC_PANEL_HOST" ]]; then
  [[ "$PUBLIC_PANEL_HOST" =~ ^[A-Za-z0-9.-]+$ && "$PUBLIC_PANEL_HOST" != -* ]] || fail 'некорректный домен/IP для публичного GUI'
  if systemctl cat m-ui.service >/dev/null 2>&1; then
    fail 'GUI уже установлен. Перенастройте его через m-ui ssl / m-ui configure.'
  fi
  PANEL_BIND='0.0.0.0'
  if [[ "$PUBLIC_PANEL_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    IFS=. read -r a b c d <<< "$PUBLIC_PANEL_HOST"
    for octet in "$a" "$b" "$c" "$d"; do
      [[ "$octet" =~ ^[0-9]{1,3}$ ]] && ((10#$octet <= 255)) || fail 'некорректный IPv4'
    done
    TLS_ARGS=(--ssl-ip "$PUBLIC_PANEL_HOST")
  else
    TLS_ARGS=(--ssl-domain "$PUBLIC_PANEL_HOST")
  fi
fi

if ((FIREWALL)); then
  [[ "$SSH_PORT" =~ ^[0-9]+$ ]] && ((SSH_PORT >= 1 && SSH_PORT <= 65535)) || fail 'укажите действующий порт SSH через --ssh-port'
  # This checks a listening TCP socket, but the SSH/cloud-firewall route must still be verified by the operator.
  command -v ss >/dev/null 2>&1 || fail 'утилита ss не найдена'
  ss -H -ltn "sport = :${SSH_PORT}" | grep -q . || fail "SSH не слушает порт ${SSH_PORT}, UFW включать небезопасно"
fi

mkdir -p "$DATA_DIR"
chmod 0700 "$DATA_DIR"
if [[ ! -f "${DATA_DIR}/before.txt" ]]; then
  {
    date -Is; uname -a
    sysctl net.ipv4.tcp_congestion_control net.core.default_qdisc net.core.somaxconn net.core.rmem_max net.core.wmem_max 2>&1 || true
    ip -br addr 2>&1 || true
  } > "${DATA_DIR}/before.txt"
  for key in net.core.default_qdisc net.ipv4.tcp_congestion_control net.core.somaxconn net.ipv4.tcp_max_syn_backlog net.core.rmem_max net.core.wmem_max; do
    value="$(sysctl -n "$key" 2>/dev/null || true)"
    [[ -n "$value" ]] && printf '%s=%s\n' "$key" "$value"
  done > "${DATA_DIR}/sysctl-before.values"
fi

log 'Устанавливаю системные зависимости'
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl openssl gzip tar procps iproute2 kmod cron socat

log 'Применяю умеренные сетевые настройки; BBR — только при поддержке ядром'
if [[ -f "$TUNING_FILE" ]] && ! grep -q '^# managed by test-tspu deploy.sh$' "$TUNING_FILE"; then
  fail "файл $TUNING_FILE уже существует и не принадлежит этому установщику"
fi
modprobe tcp_bbr 2>/dev/null || true
TUNING="# managed by test-tspu deploy.sh
# Conservative limits; measure performance before/after changes.
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
net.core.rmem_max = 8388608
net.core.wmem_max = 8388608"
if sysctl -n net.ipv4.tcp_available_congestion_control | grep -qw bbr; then
  TUNING+=$'\nnet.core.default_qdisc = fq\nnet.ipv4.tcp_congestion_control = bbr'
else
  log 'BBR ядром не поддерживается — алгоритм TCP не меняем'
fi
printf '%s\n' "$TUNING" > "$TUNING_FILE"
chmod 0644 "$TUNING_FILE"
if ! sysctl -p "$TUNING_FILE"; then
  log 'ПРЕДУПРЕЖДЕНИЕ: некоторые sysctl не применились (ограничения VPS?)'
fi

TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP_DIR"; }
trap cleanup EXIT

download_verified() {
  local url="$1" sha="$2" dest="$3"
  curl --fail --location --silent --show-error --retry 3 --connect-timeout 15 --max-time 180 "$url" -o "$dest"
  printf '%s  %s\n' "$sha" "$dest" | sha256sum --check --status || fail "не совпала контрольная сумма SHA256: $url"
}

case "$ARCH" in
  amd64)
    PANEL_SHA='25979a759b836070f7bb450fd5e04e956efc4b419dfa7ba731b06c04802d0111'
    CORE_SHA='2763f5b414527f7baf6dc57e3f82f3ab561eedf34bfd7c2f2cde1f7e592efe06'
    CORE_ASSET="mihomo-linux-amd64-compatible-${CORE_VERSION}.gz"
    ;;
  arm64)
    PANEL_SHA='29d9c07a8c5666faedbb818b2ad81308ef8a605a02b144b872a1576d19013e93'
    CORE_SHA='daaf78a6240a282cdfd75bcbf98fe321f6bd52765cc29e79f344191198f0b50f'
    CORE_ASSET="mihomo-linux-arm64-${CORE_VERSION}.gz"
    ;;
esac

mkdir -p /usr/local/m-ui
if [[ ! -x /usr/local/m-ui/mihomo ]]; then
  log "Загружаю Mihomo $CORE_VERSION с проверкой SHA256"
  download_verified "https://github.com/MetaCubeX/mihomo/releases/download/${CORE_VERSION}/${CORE_ASSET}" "$CORE_SHA" "${TMP_DIR}/mihomo.gz"
  gzip -dc "${TMP_DIR}/mihomo.gz" > "${TMP_DIR}/mihomo"
  install -m 0755 "${TMP_DIR}/mihomo" /usr/local/m-ui/mihomo
else
  log 'Mihomo уже установлен — без автоматического обновления'
fi
/usr/local/m-ui/mihomo -v | head -n 1

if ! systemctl cat m-ui.service >/dev/null 2>&1; then
  log "Загружаю m-ui $PANEL_VERSION с проверкой SHA256"
  download_verified "https://github.com/RomanovCaesar/m-ui/releases/download/${PANEL_VERSION}/m-ui-linux-${ARCH}.tar.gz" "$PANEL_SHA" "${TMP_DIR}/m-ui.tar.gz"
  mkdir -p "${TMP_DIR}/unpack"
  tar -xzf "${TMP_DIR}/m-ui.tar.gz" -C "${TMP_DIR}/unpack"
  PANEL_BIN="$(find "${TMP_DIR}/unpack" -type f -name 'm-ui' -print -quit)"
  [[ -n "$PANEL_BIN" ]] || PANEL_BIN="$(find "${TMP_DIR}/unpack" -type f -name "m-ui-linux-${ARCH}" -print -quit)"
  [[ -n "$PANEL_BIN" ]] || fail 'в архиве нет исполняемого файла m-ui'
  chmod 0755 "$PANEL_BIN"
  curl --fail --location --silent --show-error --retry 3 "$INSTALLER_URL" -o "${TMP_DIR}/m-ui-install.sh"
  MUI_BINARY_PATH="$PANEL_BIN" bash "${TMP_DIR}/m-ui-install.sh" \
    --version "$PANEL_VERSION" --skip-mihomo --listen-ip "$PANEL_BIND" --port "$PANEL_PORT" "${TLS_ARGS[@]}"
else
  log 'm-ui уже установлен — учётные данные и настройки сохраняются'
fi

mkdir -p "$DROPIN_DIR"
cat > "${DROPIN_DIR}/90-test-tspu.conf" <<'UNIT'
[Service]
LimitNOFILE=65536
TasksMax=4096
UNIT
systemctl daemon-reload
systemctl enable m-ui.service
systemctl restart m-ui.service
systemctl is-active --quiet m-ui.service || fail 'm-ui не запустился (проверьте journalctl -u m-ui -n 80)'

if ((FIREWALL)); then
  log 'Включаю UFW; порты для протоколов нужно открыть отдельно'
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ufw
  ufw allow "${SSH_PORT}/tcp" comment 'SSH - test-tspu'
  if [[ -n "$PUBLIC_PANEL_HOST" ]]; then
    ufw allow "${PANEL_PORT}/tcp" comment 'HTTPS m-ui GUI'
  fi
  ufw --force enable
fi

log 'Установка завершена.'
if [[ -n "$PUBLIC_PANEL_HOST" ]]; then
  HTTP_CODE="$(curl --insecure --silent --connect-timeout 5 --max-time 10 --output /dev/null --write-out '%{http_code}' "https://127.0.0.1:${PANEL_PORT}/" || true)"
  [[ "$HTTP_CODE" != 000 && -n "$HTTP_CODE" ]] || fail 'не удалось подтвердить HTTPS GUI, проверьте сертификат и логи m-ui'
  log "Публичная панель: https://${PUBLIC_PANEL_HOST}:${PANEL_PORT}/ (путь и пароль показаны установщиком m-ui)"
  log 'Откройте порт панели в firewall хостера, а также необходимые порты для ACME.'
else
  log 'Панель только на 127.0.0.1:2053 — доступ через SSH-туннель.'
  log 'Подключение: ssh -L 2053:127.0.0.1:2053 user@IP_СЕРВЕРА'
fi
log 'Для проверки тестового передайте адрес сервера и клиентский профиль, а не доступ к админке.'
log 'Для новых inbound откройте соответствующие TCP/UDP-порты.'
status
