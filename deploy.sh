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
DOMAIN=''
IP_OVERRIDE=''
CERT_FILE=''
KEY_FILE=''
CA_CERT_FILE=''
SELF_PATH='/usr/local/sbin/test-tspu-deploy'
INSTALLER_CACHE="${DATA_DIR}/m-ui-upstream-install.sh"
HOST_FILE="${DATA_DIR}/panel-host"
MODE_FILE="${DATA_DIR}/panel-mode"
CERT_MODE_FILE="${DATA_DIR}/cert-mode"
PUBLIC_MARK_FILE="${DATA_DIR}/panel-public"
CUSTOM_CERT="${DATA_DIR}/custom-cert.pem"
CUSTOM_KEY="${DATA_DIR}/custom-key.pem"
CHANGED=0
RESTART_REQUIRED=0

change() { log "Изменено: $*"; CHANGED=$((CHANGED + 1)); }

fail() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
log() { printf '[test-tspu] %s\n' "$*"; }

usage() {
  cat <<'HELP'
Установка: sudo bash deploy.sh [--domain example.com | --ip A.B.C.D]
Без аргументов используется автоматически обнаруженный публичный IPv4.

--domain ИМЯ         : панель по домену; запись A должна указывать на VPS.
--ip АДРЕС           : явно задать публичный IPv4 вместо автоопределения.
--cert-file ФАЙЛ     : использовать готовый TLS-сертификат (без ACME).
--key-file ФАЙЛ      : приватный ключ готового сертификата.
--ca-cert ФАЙЛ       : доверенный CA для проверки HTTPS (включая CI).
--sync-ip            : проверить смену IP и перевыпустить IP-сертификат при необходимости.
--check              : показать состояние без изменений.
--rollback-tuning    : отменить сетевые оптимизации.
--firewall --ssh-port ПОРТ : осторожно включить UFW, сохранив указанный SSH-порт.

Панель: HTTPS на TCP 2053; для выпуска сертификата нужен доступ снаружи на TCP 80.
Автопродление: acme.sh; для IP-сертификата действует короткий срок (~6 дней).
HELP
}
while (($#)); do
  case "$1" in
    --domain) (($# >= 2)) || fail 'укажите имя домена'; DOMAIN="$2"; shift 2 ;;
    --ip) (($# >= 2)) || fail 'укажите IPv4'; IP_OVERRIDE="$2"; shift 2 ;;
    --cert-file) (($# >= 2)) || fail 'укажите файл сертификата'; CERT_FILE="$2"; shift 2 ;;
    --key-file) (($# >= 2)) || fail 'укажите файл ключа'; KEY_FILE="$2"; shift 2 ;;
    --ca-cert) (($# >= 2)) || fail 'укажите файл доверенного CA'; CA_CERT_FILE="$2"; shift 2 ;;
    --sync-ip) MODE='sync-ip'; shift ;;
    --firewall) FIREWALL=1; shift ;;
    --ssh-port) (($# >= 2)) || fail 'укажите номер порта'; SSH_PORT="$2"; shift 2 ;;
    --check) MODE='check'; shift ;;
    --rollback-tuning) MODE='rollback'; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "неизвестный параметр: $1" ;;
  esac
done
[[ -z "$DOMAIN" || -z "$IP_OVERRIDE" ]] || fail 'одновременно --domain и --ip недопустимы'
[[ -z "$CERT_FILE" && -z "$KEY_FILE" || -n "$CERT_FILE" && -n "$KEY_FILE" ]] || fail '--cert-file и --key-file нужны вместе'
[[ -z "$CA_CERT_FILE" || -n "$CERT_FILE" ]] || fail '--ca-cert применяется с --cert-file'
if [[ -n "$CERT_FILE" ]]; then
  [[ -s "$CERT_FILE" && -r "$CERT_FILE" && -s "$KEY_FILE" && -r "$KEY_FILE" ]] || fail 'сертификат/ключ недоступны'
fi
[[ -z "$CA_CERT_FILE" || -s "$CA_CERT_FILE" && -r "$CA_CERT_FILE" ]] || fail 'файл CA недоступен'

is_ipv4() {
  local ip="$1" a b c d v
  [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  IFS=. read -r a b c d <<< "$ip"
  for v in "$a" "$b" "$c" "$d"; do
    [[ "$v" =~ ^[0-9]{1,3}$ ]] && ((10#$v <= 255)) || return 1
  done
  return 0
}

# Отсекаем локальные, тестовые и зарезервированные диапазоны адресов.
is_public_ipv4() {
  is_ipv4 "$1" || return 1
  local a b c d
  IFS=. read -r a b c d <<< "$1"
  a=$((10#$a)); b=$((10#$b)); c=$((10#$c))
  (( a >= 1 && a <= 223 )) || return 1
  (( a != 10 && a != 127 )) || return 1
  (( !(a == 172 && b >= 16 && b <= 31) )) || return 1
  (( !(a == 192 && b == 168) )) || return 1
  (( !(a == 169 && b == 254) )) || return 1
  (( !(a == 100 && b >= 64 && b <= 127) )) || return 1
  (( !(a == 198 && b >= 18 && b <= 19) )) || return 1
  (( !(a == 192 && b == 0 && (c == 0 || c == 2)) )) || return 1
  (( !(a == 198 && b == 51 && c == 100) )) || return 1
  (( !(a == 203 && b == 0 && c == 113) )) || return 1
}

detect_ip() {
  local ip url
  for url in https://api4.ipify.org https://ipv4.icanhazip.com; do
    ip="$(curl -4fsS --connect-timeout 5 --max-time 10 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
    if is_public_ipv4 "$ip"; then printf '%s\n' "$ip"; return 0; fi
  done
  return 1
}

validate_domain() {
  [[ "$1" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*[a-zA-Z0-9]$ && "$1" == *.* && ${#1} -le 253 ]] || fail 'некорректное имя домена'
}

get_target() {
  if [[ -n "$DOMAIN" ]]; then
    validate_domain "$DOMAIN"
    TARGET_MODE='domain'; TARGET_HOST="$DOMAIN"
  elif [[ -n "$IP_OVERRIDE" ]]; then
    is_public_ipv4 "$IP_OVERRIDE" || fail 'нужен публичный IPv4'
    TARGET_MODE='ip'; TARGET_HOST="$IP_OVERRIDE"
  elif [[ -f "$MODE_FILE" && "$(cat "$MODE_FILE")" == domain && -f "$HOST_FILE" ]]; then
    TARGET_MODE='domain'; TARGET_HOST="$(cat "$HOST_FILE")"
    validate_domain "$TARGET_HOST"
  else
    TARGET_MODE='ip'; TARGET_HOST="$(detect_ip)" || fail 'не удалось узнать публичный IPv4; укажите --ip'
  fi
}

# Обновление сертификата: выполняется только если IP действительно поменялся.
sync_ip() {
  require_root
  [[ -r "$MODE_FILE" && -r "$HOST_FILE" ]] || fail 'установка ещё не завершена'
  [[ "$(cat "$MODE_FILE")" == ip ]] || { log 'Для домена сертификат привязан к имени; обновите DNS при смене IP.'; return 0; }
  [[ -r "$INSTALLER_CACHE" ]] || fail "не найден $INSTALLER_CACHE"
  local next old
  old="$(cat "$HOST_FILE")"
  next="${IP_OVERRIDE:-$(detect_ip)}" || fail 'не получилось определить новый IP'
  is_public_ipv4 "$next" || fail 'нужен публичный IPv4'
  if [[ "$old" == "$next" ]]; then log "IP не изменился: $next"; return 0; fi
  if ss -H -ltn 'sport = :80' | grep -q .; then fail 'TCP 80 занят — ACME HTTP-01 не сможет пройти; освободите порт'; fi
  log "Новый IP: $old → $next. Запрашиваю сертификат Let's Encrypt..."
  if bash "$INSTALLER_CACHE" --ssl-menu --ssl-ip "$next"; then
    [[ -s /root/cert/ip/fullchain.pem ]] || fail 'не обнаружен установленный сертификат'
    printf '%s\n' "$next" > "${HOST_FILE}.new"
    mv -f "${HOST_FILE}.new" "$HOST_FILE"
    if [[ -x /root/.acme.sh/acme.sh ]]; then
      /root/.acme.sh/acme.sh --remove -d "$old" >/dev/null 2>&1 || log 'Не удалось исключить старый IP из автопродления.'
    fi
    log "Новый сертификат установлен: https://${next}:2053/"
  else
    fail "Не удалось выпустить сертификат на $next. Старый адрес сохранён; следующая попытка по таймеру."
  fi
}

require_root() { ((EUID == 0)) || fail 'запустите от root: sudo bash deploy.sh'; }

status() {
  log "Ядро Linux: $(uname -r)"
  if [[ -r "$MODE_FILE" && -r "$HOST_FILE" ]]; then log "Адрес панели: https://$(cat "$HOST_FILE"):2053/ ($(cat "$MODE_FILE"))"; fi
  log "TCP congestion control: $(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo n/a)"
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
if [[ "$MODE" == sync-ip ]]; then sync_ip; exit 0; fi
require_root
mkdir -p "$DATA_DIR"
chmod 0700 "$DATA_DIR"
exec 9>"${DATA_DIR}/deploy.lock"
flock -n 9 || fail 'другой деплой уже выполняется'
TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP_DIR"; }
trap cleanup EXIT

write_if_changed() {
  local src="$1" dest="$2" mode="${3:-0644}"
  if [[ -f "$dest" ]] && cmp -s "$src" "$dest"; then return 1; fi
  install -m "$mode" "$src" "${dest}.new"
  mv -f "${dest}.new" "$dest"
  change "$dest"
  return 0
}

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


if ((FIREWALL)); then
  [[ "$SSH_PORT" =~ ^[0-9]+$ ]] && ((SSH_PORT >= 1 && SSH_PORT <= 65535)) || fail 'укажите действующий порт SSH через --ssh-port'
  # This checks a listening TCP socket, but the SSH/cloud-firewall route must still be verified by the operator.
  command -v ss >/dev/null 2>&1 || fail 'утилита ss не найдена'
  ss -H -ltn "sport = :${SSH_PORT}" | grep -q . || fail "SSH не слушает порт ${SSH_PORT}, UFW включать небезопасно"
fi

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

log 'Проверяю системные зависимости'
missing_packages=()
for dep in ca-certificates curl openssl gzip tar procps iproute2 kmod cron socat; do
  if ! dpkg -s "$dep" >/dev/null 2>&1; then missing_packages+=("$dep"); fi
done
if ((${#missing_packages[@]})); then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing_packages[@]}"
  change "установлены пакеты: ${missing_packages[*]}"
fi
# Определяем внешний IP только после установки curl.
get_target
log "TLS-адрес панели: $TARGET_HOST ($TARGET_MODE)"
# Если UFW уже включён — открываем только порты панели и ACME.
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw allow 80/tcp comment 'ACME test-tspu'
  ufw allow "${PANEL_PORT}/tcp" comment 'HTTPS m-ui test-tspu'
fi
if [[ "$TARGET_MODE" == domain && -z "$CERT_FILE" ]]; then
  # Проверка информативна: DNS-провайдер может возвращать несколько адресов.
  PUBLIC_IP="$(detect_ip)" || fail 'не удаётся определить IP для проверки DNS'
  if ! getent ahostsv4 "$TARGET_HOST" | awk '{print $1}' | grep -Fx "$PUBLIC_IP" >/dev/null; then
    fail "A-запись $TARGET_HOST не указывает на текущий публичный IP VPS. Проверьте DNS до выпуска TLS."
  fi
fi


log 'Проверяю сетевые настройки (BBR и лимиты)'
if [[ -f "$TUNING_FILE" ]] && ! grep -q '^# managed by test-tspu deploy.sh$' "$TUNING_FILE"; then
  fail "файл $TUNING_FILE уже существует и не принадлежит этому установщику"
fi
modprobe tcp_bbr 2>/dev/null || true
TUNING="# managed by test-tspu deploy.sh
# Параметры изменяются только при отличии от требуемых значений.
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
net.core.rmem_max = 8388608
net.core.wmem_max = 8388608"
if sysctl -n net.ipv4.tcp_available_congestion_control | grep -qw bbr; then
  TUNING+=$'\nnet.core.default_qdisc = fq\nnet.ipv4.tcp_congestion_control = bbr'
else
  log 'BBR недоступен: оставляю действующий алгоритм TCP'
fi
printf '%s\n' "$TUNING" > "${TMP_DIR}/tuning.conf"
TUNING_CHANGED=0
if write_if_changed "${TMP_DIR}/tuning.conf" "$TUNING_FILE"; then TUNING_CHANGED=1; fi
SYSCTL_DRIFT=0
while IFS='=' read -r key value; do
  key="${key//[[:space:]]/}"
  value="${value//[[:space:]]/}"
  [[ -n "$key" && "${key:0:1}" != '#' ]] || continue
  if [[ "$(sysctl -n "$key" 2>/dev/null || true)" != "$value" ]]; then
    SYSCTL_DRIFT=1
    break
  fi
done < "${TMP_DIR}/tuning.conf"
if ((TUNING_CHANGED || SYSCTL_DRIFT)); then
  if ! sysctl -p "$TUNING_FILE"; then
    log 'ПРЕДУПРЕЖДЕНИЕ: некоторые sysctl не применились (ограничения VPS)'
  fi
  if ((SYSCTL_DRIFT && !TUNING_CHANGED)); then change 'восстановлены текущие значения sysctl'; fi
fi

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


CORE_PATH='/usr/local/m-ui/mihomo'
PANEL_PATH='/usr/local/m-ui/m-ui'
PANEL_DATA='/usr/local/m-ui/data'
CORE_UPDATED=0
PANEL_BOOTSTRAP=0
mkdir -p /usr/local/m-ui

CORE_INSTALLED="$("$CORE_PATH" -v 2>/dev/null | head -n1 || true)"
PANEL_INSTALLED="$("$PANEL_PATH" version 2>/dev/null | head -n1 || true)"
NEED_CORE=0
NEED_PANEL=0
[[ "$CORE_INSTALLED" == *"$CORE_VERSION"* ]] || NEED_CORE=1
[[ "$PANEL_INSTALLED" == *"$PANEL_VERSION"* ]] || NEED_PANEL=1
if ! systemctl cat m-ui.service >/dev/null 2>&1 || [[ ! -s "$PANEL_DATA/state.json" ]]; then
  PANEL_BOOTSTRAP=1
  NEED_PANEL=1
fi

# Сначала получаем и проверяем всё, что требуется обновить. Работающие файлы пока не меняем.
if ((NEED_CORE)); then
  log "Подготавливаю Mihomo $CORE_VERSION (текущая версия: ${CORE_INSTALLED:-нет})"
  download_verified "https://github.com/MetaCubeX/mihomo/releases/download/${CORE_VERSION}/${CORE_ASSET}" "$CORE_SHA" "${TMP_DIR}/mihomo.gz"
  gzip -dc "${TMP_DIR}/mihomo.gz" > "${TMP_DIR}/mihomo"
  chmod 0755 "${TMP_DIR}/mihomo"
  [[ "$("${TMP_DIR}/mihomo" -v 2>/dev/null)" == *"$CORE_VERSION"* ]] || fail 'версия скачанного Mihomo не совпала'
else
  log "Без изменений: Mihomo $CORE_VERSION"
fi
if ((NEED_PANEL)); then
  log "Подготавливаю m-ui $PANEL_VERSION (текущая версия: ${PANEL_INSTALLED:-нет})"
  download_verified "https://github.com/RomanovCaesar/m-ui/releases/download/${PANEL_VERSION}/m-ui-linux-${ARCH}.tar.gz" "$PANEL_SHA" "${TMP_DIR}/m-ui.tar.gz"
  mkdir -p "${TMP_DIR}/unpack"
  tar -xzf "${TMP_DIR}/m-ui.tar.gz" -C "${TMP_DIR}/unpack"
  PANEL_BIN="$(find "${TMP_DIR}/unpack" -type f -name 'm-ui' -print -quit)"
  [[ -n "$PANEL_BIN" ]] || PANEL_BIN="$(find "${TMP_DIR}/unpack" -type f -name "m-ui-linux-${ARCH}" -print -quit)"
  [[ -n "$PANEL_BIN" ]] || fail 'в архиве нет исполняемого файла m-ui'
  chmod 0755 "$PANEL_BIN"
  [[ "$("$PANEL_BIN" version 2>/dev/null)" == *"$PANEL_VERSION"* ]] || fail 'версия скачанной m-ui не совпала'
else
  log "Без изменений: m-ui $PANEL_VERSION"
fi
if [[ ! -s "$INSTALLER_CACHE" ]]; then
  download_tmp="${TMP_DIR}/upstream-install.sh"
  curl --fail --location --silent --show-error --retry 3 "$INSTALLER_URL" -o "$download_tmp"
  install -m 0600 "$download_tmp" "$INSTALLER_CACHE"
  change 'сохранён установщик m-ui для управления сертификатами'
fi

# Core обновляем атомарной заменой; предыдущий бинарник возвращается, если запуск не удался.
if ((NEED_CORE)); then
  if [[ -x "$CORE_PATH" ]]; then cp -a "$CORE_PATH" "${TMP_DIR}/mihomo.old"; fi
  install -m 0755 "${TMP_DIR}/mihomo" "${CORE_PATH}.new"
  mv -f "${CORE_PATH}.new" "$CORE_PATH"
  CORE_UPDATED=1
  RESTART_REQUIRED=1
  change "Mihomo → $CORE_VERSION"
fi
rollback_core() {
  if ((CORE_UPDATED)) && [[ -s "${TMP_DIR}/mihomo.old" ]]; then
    install -m 0755 "${TMP_DIR}/mihomo.old" "${CORE_PATH}.rollback"
    mv -f "${CORE_PATH}.rollback" "$CORE_PATH"
    systemctl reset-failed m-ui.service 2>/dev/null || true
    systemctl restart m-ui.service || true
    log 'Откатил бинарник Mihomo после ошибки'
  fi
}
if ((NEED_PANEL)); then
  if ((PANEL_BOOTSTRAP)); then
    if ! MUI_BINARY_PATH="$PANEL_BIN" bash "$INSTALLER_CACHE" \
      --version "$PANEL_VERSION" --skip-mihomo --listen-ip 127.0.0.1 --port "$PANEL_PORT" --no-ssl; then
      rollback_core
      fail 'первичная установка m-ui завершилась ошибкой'
    fi
  else
    # Upstream сохраняет резервную копию state.json и старый бинарник перед обновлением.
    if ! MUI_BINARY_PATH="$PANEL_BIN" bash "$INSTALLER_CACHE" \
      --update --version "$PANEL_VERSION" --skip-mihomo --no-ssl; then
      rollback_core
      fail 'не удалось обновить m-ui; проверьте резервную копию в data/backups'
    fi
  fi
  change "m-ui → $PANEL_VERSION"
fi
[[ "$("$CORE_PATH" -v 2>/dev/null)" == *"$CORE_VERSION"* ]] || fail 'после установки версия Mihomo неверная'
[[ "$("$PANEL_PATH" version 2>/dev/null)" == *"$PANEL_VERSION"* ]] || fail 'после установки версия m-ui неверная'

# Системные лимиты и сервис меняем только при различиях.
mkdir -p "$DROPIN_DIR"
cat > "${TMP_DIR}/limits.conf" <<'UNIT'
[Service]
LimitNOFILE=65536
TasksMax=4096
UNIT
SYSTEMD_CHANGED=0
if write_if_changed "${TMP_DIR}/limits.conf" "${DROPIN_DIR}/90-test-tspu.conf"; then
  SYSTEMD_CHANGED=1
  RESTART_REQUIRED=1
fi
if ((SYSTEMD_CHANGED)); then systemctl daemon-reload; fi
if ! systemctl is-enabled --quiet m-ui.service; then
  systemctl enable m-ui.service
  change 'включён автозапуск m-ui'
fi
if ! systemctl is-active --quiet m-ui.service; then
  systemctl reset-failed m-ui.service 2>/dev/null || true
  if ! systemctl start m-ui.service; then rollback_core; fail 'm-ui не запустился'; fi
  change 'запущен m-ui'
elif ((RESTART_REQUIRED)); then
  systemctl reset-failed m-ui.service 2>/dev/null || true
  if ! systemctl restart m-ui.service; then rollback_core; fail 'перезапуск m-ui завершился ошибкой'; fi
  change 'перезапущен m-ui после изменения бинарника/лимитов'
fi
systemctl is-active --quiet m-ui.service || fail 'm-ui не работает'

PREV_MODE="$(cat "$MODE_FILE" 2>/dev/null || true)"
PREV_HOST="$(cat "$HOST_FILE" 2>/dev/null || true)"
PREV_CERT_MODE="$(cat "$CERT_MODE_FILE" 2>/dev/null || true)"
TLS_UPDATE=0
if [[ "$TARGET_MODE" != "$PREV_MODE" || "$TARGET_HOST" != "$PREV_HOST" ]]; then
  TLS_UPDATE=1
elif [[ -n "$CERT_FILE" ]] && { [[ "$PREV_CERT_MODE" != custom ]] || \
      ! cmp -s "$CERT_FILE" "$CUSTOM_CERT" || ! cmp -s "$KEY_FILE" "$CUSTOM_KEY"; }; then
  TLS_UPDATE=1
elif [[ "$TARGET_MODE" == ip && "$PREV_CERT_MODE" != custom && ! -s /root/cert/ip/fullchain.pem ]]; then
  TLS_UPDATE=1
fi
if ((TLS_UPDATE)); then
  if [[ -n "$CERT_FILE" ]]; then
    log "Применяю TLS-сертификат: $TARGET_HOST"
    if ! cmp -s "$CERT_FILE" "$CUSTOM_CERT"; then
      write_if_changed "$CERT_FILE" "$CUSTOM_CERT" 0644 || true
    fi
    if ! cmp -s "$KEY_FILE" "$CUSTOM_KEY"; then
      write_if_changed "$KEY_FILE" "$CUSTOM_KEY" 0600 || true
    fi
    bash "$INSTALLER_CACHE" --ssl-menu --cert "$CUSTOM_CERT" --key "$CUSTOM_KEY" ||
      fail 'не удалось применить TLS-сертификат; проверьте настройки m-ui'
  else
    if ss -H -ltn 'sport = :80' | grep -q .; then fail 'TCP 80 занят — ACME HTTP-01 не сможет пройти'; fi
    log "Выпускаю TLS-сертификат: $TARGET_HOST"
    if [[ "$TARGET_MODE" == ip ]]; then
      bash "$INSTALLER_CACHE" --ssl-menu --ssl-ip "$TARGET_HOST" || fail 'не удалось выпустить IP-сертификат'
    else
      bash "$INSTALLER_CACHE" --ssl-menu --ssl-domain "$TARGET_HOST" || fail 'не удалось выпустить сертификат домена'
    fi
  fi
  printf '%s\n' "$TARGET_MODE" > "${MODE_FILE}.new"
  printf '%s\n' "$TARGET_HOST" > "${HOST_FILE}.new"
  mv -f "${MODE_FILE}.new" "$MODE_FILE"
  mv -f "${HOST_FILE}.new" "$HOST_FILE"
  if [[ -n "$CERT_FILE" ]]; then printf 'custom\n' > "$CERT_MODE_FILE"; else printf 'acme\n' > "$CERT_MODE_FILE"; fi
  change "HTTPS для $TARGET_HOST"
else
  log "Без изменений: TLS для $TARGET_HOST"
fi

# Административная панель публикуется только после установки HTTPS.
# Проверяем реальный listening socket, чтобы исправить drift даже при наличии marker.
PUBLIC_LISTEN=0
if ss -H -ltn 'sport = :2053' | grep -Eq '0[.]0[.]0[.]0:2053|\*:2053|\[::\]:2053'; then
  PUBLIC_LISTEN=1
fi
if [[ ! -f "$PUBLIC_MARK_FILE" ]] || (( !PUBLIC_LISTEN )); then
  systemctl stop m-ui.service
  "$PANEL_PATH" configure --data-dir "$PANEL_DATA" --listen-ip 0.0.0.0 >/dev/null
  systemctl reset-failed m-ui.service 2>/dev/null || true
  if ! systemctl start m-ui.service; then
    rollback_core
    fail 'm-ui не запустился после настройки публичного HTTPS'
  fi
  printf '%s\n' '0.0.0.0' > "$PUBLIC_MARK_FILE"
  change 'GUI слушает публичный интерфейс по HTTPS'
fi

# Для проверки IP после перезапуска используется собственная актуальная копия скрипта.
[[ -f "${BASH_SOURCE[0]}" ]] || fail 'запускайте скачанный файл, не передавайте код через stdin'
write_if_changed "${BASH_SOURCE[0]}" "$SELF_PATH" 0700 || true

cat > "${TMP_DIR}/ip-check.service" <<'UNIT'
[Unit]
Description=Проверка IP для сертификата m-ui
After=network-online.target m-ui.service
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/test-tspu-deploy --sync-ip
UNIT
cat > "${TMP_DIR}/ip-check.timer" <<'UNIT'
[Unit]
Description=Проверка смены IP и обновление HTTPS-панели
[Timer]
OnBootSec=3min
OnUnitActiveSec=30min
RandomizedDelaySec=3min
Persistent=true
[Install]
WantedBy=timers.target
UNIT
TIMER_CHANGED=0
if write_if_changed "${TMP_DIR}/ip-check.service" /etc/systemd/system/test-tspu-ip-check.service; then TIMER_CHANGED=1; fi
if write_if_changed "${TMP_DIR}/ip-check.timer" /etc/systemd/system/test-tspu-ip-check.timer; then TIMER_CHANGED=1; fi
if ((TIMER_CHANGED)); then systemctl daemon-reload; fi
if ! systemctl is-enabled --quiet test-tspu-ip-check.timer; then
  systemctl enable test-tspu-ip-check.timer
  change 'включён таймер проверки IP'
fi
if ! systemctl is-active --quiet test-tspu-ip-check.timer; then
  systemctl start test-tspu-ip-check.timer
  change 'запущен таймер проверки IP'
elif ((TIMER_CHANGED)); then
  systemctl restart test-tspu-ip-check.timer
  change 'перезапущен таймер после изменения конфигурации'
fi


if ((FIREWALL)); then
  log 'Включаю UFW; порты для протоколов нужно открыть отдельно'
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ufw
  ufw allow "${SSH_PORT}/tcp" comment 'SSH - test-tspu'
  ufw allow 80/tcp comment 'ACME HTTPS test-tspu'
  ufw allow "${PANEL_PORT}/tcp" comment 'HTTPS m-ui GUI'
  ufw --force enable
fi

# Для предотвращения ложного успеха проверяем TLS с корректным именем/IP,
# а не просто ответ на 127.0.0.1 с --insecure.
TLS_CURL_ARGS=()
if [[ -n "$CA_CERT_FILE" ]]; then TLS_CURL_ARGS=(--cacert "$CA_CERT_FILE"); fi
if ! curl "${TLS_CURL_ARGS[@]}" --noproxy '*' -sS --retry 6 --retry-connrefused --retry-delay 1 --connect-timeout 3 --max-time 3 --resolve "${TARGET_HOST}:${PANEL_PORT}:127.0.0.1" "https://${TARGET_HOST}:${PANEL_PORT}/" -o /dev/null; then
  fail "HTTPS на ${TARGET_HOST}:${PANEL_PORT} не прошёл проверку. Проверьте TLS и DNS."
fi
log "Готово: https://${TARGET_HOST}:${PANEL_PORT}/"
log 'Путь, логин и пароль выведены установщиком m-ui. Сохраните их.'
log 'Откройте во внешнем firewall TCP 80 (сертификаты) и TCP 2053 (GUI).'
log 'Для изменения IP можно повторить установку с --ip АДРЕС или дождаться проверки по таймеру.'
log 'Клиентские подключения и их порты создаются отдельно в GUI.'
if ((CHANGED == 0)); then
  log 'Итог: без изменений, работающие службы не перезапускались'
else
  log "Итог: применено изменений: $CHANGED"
fi
status