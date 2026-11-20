# test_tspu — установка Mihomo + m-ui

Установка на чистую **Ubuntu 22.04/24.04 LTS** (amd64/arm64). Docker и `git clone` не нужны.

**Один файл для запуска — `deploy.sh`.** Скрипт сам скачивает и проверяет Mihomo `v1.19.34` и m-ui `v0.3.3`, настраивает автозапуск через systemd, а также BBR/fq и умеренные системные лимиты.

## Скачать и запустить

Если репозиторий станет публичным:

```bash
curl -fsSL https://raw.githubusercontent.com/detektor133/test_tspu/main/deploy.sh -o deploy.sh
sudo bash deploy.sh
```

**Сейчас репозиторий приватный.** Нужен GitHub-токен с правом читать его содержимое (или скачайте один `deploy.sh` вручную):

```bash
read -rsp "GitHub-токен: " GH_TOKEN; echo
curl -fsSL \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github.raw+json" \
  https://api.github.com/repos/detektor133/test_tspu/contents/deploy.sh -o deploy.sh
unset GH_TOKEN
sudo bash deploy.sh
```

## Как открывать GUI

Без параметров GUI слушает только `127.0.0.1:2053` (управление через SSH-туннель).

Чтобы **сразу при первой установке** открыть GUI из интернета с HTTPS и авторизацией:

```bash
sudo bash deploy.sh --public-panel vpn.example.com
```

Можно вместо домена указать публичный IPv4; для него панель попробует выпустить краткосрочный TLS-сертификат. Нужны доступный домен/IP, рабочее получение сертификата ACME, а также необходимые правила firewall хостера. Публичная админка менее безопасна, чем SSH-доступ. Пароль и случайный путь панели создаёт m-ui.

**Проверяющий не должен входить в админку.** Согласно тестовому заданию ему передают адрес сервера и клиентский профиль (ссылку/QR), который создаётся в GUI.

## Проверка настроек

```bash
sudo bash deploy.sh --check
sudo bash deploy.sh --rollback-tuning
sudo journalctl -u m-ui -n 80 --no-pager
```

UFW по умолчанию не включаем, чтобы случайно не потерять SSH. Опционально: `sudo bash deploy.sh --firewall --ssh-port 22` (только если SSH использует 22). Порты протоколов, созданных через GUI, открываются отдельно.

BBR относится к **TCP** и не ускоряет Hysteria2/TUIC по UDP напрямую. Влияние сетевых параметров на производительность ещё предстоит измерить.

**Статус:** синтаксис и параметры установщика проверены, но запуск на реальном VPS и выдача TLS-сертификата ещё не проверены.

Источники: [m-ui](https://github.com/RomanovCaesar/m-ui), [Mihomo](https://github.com/MetaCubeX/mihomo), [BBR](https://github.com/google/bbr).
