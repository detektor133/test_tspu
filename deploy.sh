#!/usr/bin/env bash
# Native Ubuntu installer for Mihomo + m-ui, with conservative kernel tuning.
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

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[test-tspu] %s\n' "$*"; }

usage() {
  cat <<'HELP'
Usage: sudo bash deploy.sh [--firewall --ssh-port PORT] [--check | --rollback-tuning]

Default: install m-ui + Mihomo natively and apply conservative kernel tuning.
--firewall: enable UFW (ONLY with explicitly supplied active SSH port).
--check: print current settings and service state; change nothing.
--rollback-tuning: remove our sysctl changes and systemd limits.

Panel listens only on 127.0.0.1:2053; use SSH local port forwarding.
HELP
}

while (($#)); do
  case "$1" in
    --firewall) FIREWALL=1; shift ;;
    --ssh-port) (($# >= 2)) || fail '--ssh-port requires a number'; SSH_PORT="$2"; shift 2 ;;
    --check) MODE='check'; shift ;;
    --rollback-tuning) MODE='rollback'; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

require_root() { ((EUID == 0)) || fail 'run as root: sudo bash deploy.sh'; }

status() {
  log "kernel: $(uname -r)"
  log "congestion-control: $(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo n/a)"
  log "qdisc: $(sysctl -n net.core.default_qdisc 2>/dev/null || echo n/a)"
  log "somaxconn: $(sysctl -n net.core.somaxconn 2>/dev/null || echo n/a)"
  log "UDP rmem_max: $(sysctl -n net.core.rmem_max 2>/dev/null || echo n/a)"
  log "UDP wmem_max: $(sysctl -n net.core.wmem_max 2>/dev/null || echo n/a)"
  if command -v systemctl >/dev/null 2>&1; then
    log "m-ui service: $(systemctl is-active m-ui.service 2>/dev/null || true)"
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
      fail "refusing to remove modified $TUNING_FILE"
    fi
    if [[ -f "${DATA_DIR}/sysctl-before.values" ]]; then
      while IFS='=' read -r key value; do
        [[ -n "$key" && -n "$value" ]] || continue
        sysctl -w "$key=$value" >/dev/null || log "WARNING: could not restore $key"
      done < "${DATA_DIR}/sysctl-before.values"
    fi
  fi
  if [[ -f "${DROPIN_DIR}/90-test-tspu.conf" ]]; then
    rm -f "${DROPIN_DIR}/90-test-tspu.conf"
    systemctl daemon-reload
    systemctl try-restart m-ui.service || true
  fi
  log 'tuning removed; panel and Mihomo were not uninstalled'
}

if [[ "$MODE" == check ]]; then status; exit 0; fi
if [[ "$MODE" == rollback ]]; then rollback; exit 0; fi
require_root

[[ -r /etc/os-release ]] || fail 'cannot determine OS'
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || fail 'supported OS: Ubuntu 22.04 or 24.04 LTS'
[[ "${VERSION_ID:-}" == '22.04' || "${VERSION_ID:-}" == '24.04' ]] || fail 'supported Ubuntu: 22.04 or 24.04 LTS'
command -v systemctl >/dev/null 2>&1 || fail 'systemd is required'
case "$(uname -m)" in
  x86_64) ARCH='amd64' ;;
  aarch64) ARCH='arm64' ;;
  *) fail 'supported CPU: amd64 or arm64' ;;
esac

if ((FIREWALL)); then
  [[ "$SSH_PORT" =~ ^[0-9]+$ ]] && ((SSH_PORT >= 1 && SSH_PORT <= 65535)) || fail 'specify --ssh-port with your actual SSH port'
  # This checks a listening TCP socket, but the SSH/cloud-firewall route must still be verified by the operator.
  command -v ss >/dev/null 2>&1 || fail 'ss command missing'
  ss -H -ltn "sport = :${SSH_PORT}" | grep -q . || fail "no TCP listener on SSH port ${SSH_PORT}; refusing firewall change"
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

log 'installing base dependencies'
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl openssl gzip tar procps iproute2 kmod cron socat

log 'applying kernel settings (BBR only when supported)'
if [[ -f "$TUNING_FILE" ]] && ! grep -q '^# managed by test-tspu deploy.sh$' "$TUNING_FILE"; then
  fail "existing $TUNING_FILE belongs to someone else; will not overwrite"
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
  log 'BBR unavailable in kernel; leaving default TCP congestion control unchanged'
fi
printf '%s\n' "$TUNING" > "$TUNING_FILE"
chmod 0644 "$TUNING_FILE"
if ! sysctl -p "$TUNING_FILE"; then
  log 'WARNING: some sysctl parameters could not be applied (container or restricted VPS?)'
fi

TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP_DIR"; }
trap cleanup EXIT

download_verified() {
  local url="$1" sha="$2" dest="$3"
  curl --fail --location --silent --show-error --retry 3 --connect-timeout 15 --max-time 180 "$url" -o "$dest"
  printf '%s  %s\n' "$sha" "$dest" | sha256sum --check --status || fail "SHA256 mismatch: $url"
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
  log "downloading verified Mihomo $CORE_VERSION"
  download_verified "https://github.com/MetaCubeX/mihomo/releases/download/${CORE_VERSION}/${CORE_ASSET}" "$CORE_SHA" "${TMP_DIR}/mihomo.gz"
  gzip -dc "${TMP_DIR}/mihomo.gz" > "${TMP_DIR}/mihomo"
  install -m 0755 "${TMP_DIR}/mihomo" /usr/local/m-ui/mihomo
else
  log 'existing Mihomo preserved (no implicit upgrades)'
fi
/usr/local/m-ui/mihomo -v | head -n 1

if ! systemctl cat m-ui.service >/dev/null 2>&1; then
  log "installing verified m-ui $PANEL_VERSION"
  download_verified "https://github.com/RomanovCaesar/m-ui/releases/download/${PANEL_VERSION}/m-ui-linux-${ARCH}.tar.gz" "$PANEL_SHA" "${TMP_DIR}/m-ui.tar.gz"
  mkdir -p "${TMP_DIR}/unpack"
  tar -xzf "${TMP_DIR}/m-ui.tar.gz" -C "${TMP_DIR}/unpack"
  PANEL_BIN="$(find "${TMP_DIR}/unpack" -type f -name 'm-ui' -print -quit)"
  [[ -n "$PANEL_BIN" ]] || PANEL_BIN="$(find "${TMP_DIR}/unpack" -type f -name "m-ui-linux-${ARCH}" -print -quit)"
  [[ -n "$PANEL_BIN" ]] || fail 'm-ui binary missing in verified archive'
  chmod 0755 "$PANEL_BIN"
  curl --fail --location --silent --show-error --retry 3 "$INSTALLER_URL" -o "${TMP_DIR}/m-ui-install.sh"
  MUI_BINARY_PATH="$PANEL_BIN" bash "${TMP_DIR}/m-ui-install.sh" \
    --version "$PANEL_VERSION" --skip-mihomo --listen-ip 127.0.0.1 --port "$PANEL_PORT" --no-ssl
else
  log 'existing m-ui service preserved (no credential or UI reset)'
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
systemctl is-active --quiet m-ui.service || fail 'm-ui service failed (journalctl -u m-ui -n 80)'

if ((FIREWALL)); then
  log 'enabling UFW; new VPN inbound ports must be explicitly allowed later'
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ufw
  ufw allow "${SSH_PORT}/tcp" comment 'SSH - test-tspu'
  ufw --force enable
fi

log 'install complete. UI only on 127.0.0.1:2053.'
log 'SSH tunnel: ssh -L 2053:127.0.0.1:2053 user@VPS_IP'
log 'Use the generated panel path/credentials printed by the upstream installer.'
log 'NOTE: for each inbound, open its TCP/UDP port in provider firewall and (if enabled) UFW.'
status