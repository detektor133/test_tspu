#!/usr/bin/env bash
# Установка клиентской части на Android/Termux. НЕ использует root, VPN Android и USB tethering.
set -Eeuo pipefail
umask 077

if [[ -z "${TERMUX_VERSION:-}" && "${PREFIX:-}" != *com.termux* ]]; then
  echo 'Запустите этот скрипт внутри приложения Termux на Android.' >&2
  exit 1
fi
if [[ "$(uname -m)" != aarch64 ]]; then
  echo 'Автоматический бинарник поддерживает только Android ARM64 (aarch64).' >&2
  exit 1
fi

pkg update -y
pkg install -y python python-pip openssh curl git gzip ca-certificates
python -m pip install 'PyYAML>=6,<7'
mkdir -p "$HOME/.local/bin" "$HOME/.config/test-tspu"
chmod 700 "$HOME/.config/test-tspu"
VERSION='v1.19.34'
EXPECTED='48a71fc6a68e2909587176e0ab7185d8f2fac298fa96ccdacf30ea86ab2359b5'
URL="https://github.com/MetaCubeX/mihomo/releases/download/$VERSION/mihomo-android-arm64-v8-$VERSION.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Загружаю официальный Android Mihomo $VERSION (ARM64)"
curl --fail --location --retry 2 --connect-timeout 15 "$URL" -o "$TMP/mihomo.gz"
printf '%s  %s\n' "$EXPECTED" "$TMP/mihomo.gz" | sha256sum -c -
gzip -dc "$TMP/mihomo.gz" > "$TMP/mihomo"
chmod 0700 "$TMP/mihomo"
"$TMP/mihomo" -v
mv -f "$TMP/mihomo" "$HOME/.local/bin/mihomo"
chmod 0700 "$HOME/.local/bin/mihomo"
echo
echo 'Mihomo установлен в ~/.local/bin/mihomo.'
echo 'Следующий шаг: python bench/phone.py configure'
echo 'Далее: python bench/phone.py run --vps ВАШ_IP --network sbermobile'
echo 'Нужно один раз настроить SSH-ключ Termux для доступа к своему VPS.'
echo 'Ни USB-модем, ни root на телефоне НЕ нужны.'
