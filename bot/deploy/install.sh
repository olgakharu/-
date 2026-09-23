#!/usr/bin/env bash
# Установка и обновление бота на сервере (Ubuntu / Debian).
#
# Установка:   curl -fsSL <ссылка на этот файл> | sudo bash
# Обновление:  тот же запуск — код обновится, настройки и база сохранятся.
# Поменять настройки:  sudo bash /opt/tochka-bot/app/bot/deploy/install.sh --reconfigure
# Тексты (content.json) правь в GitHub, затем запусти обновление — правки на сервере затрутся.
set -euo pipefail

REPO="https://github.com/olgakharu/-.git"
BRANCH="${BRANCH:-claude/friendly-ride-qsxvyk}"
ROOT="/opt/tochka-bot"
APP="$ROOT/app"
BOT="$APP/bot"
SERVICE="tochka-bot"
RUN_USER="tochkabot"

say()  { printf '\n\033[1;33m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$*"; exit 1; }
ask()  { local v; read -r -p "$1" v </dev/tty; printf '%s' "$v"; }

[ "$(id -u)" -eq 0 ] || fail "Запусти через sudo"

say "Ставлю системные пакеты"
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || fail "Нужен Python 3.10+, на сервере $(python3 -V). Обнови систему до Ubuntu 22.04+"
ok "$(python3 -V)"

id "$RUN_USER" >/dev/null 2>&1 || useradd --system --home "$ROOT" --shell /usr/sbin/nologin "$RUN_USER"
mkdir -p "$ROOT"

git config --global --add safe.directory "$APP" 2>/dev/null || true
say "Загружаю код бота (ветка $BRANCH)"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q origin "$BRANCH"
  git -C "$APP" checkout -q "$BRANCH"
  git -C "$APP" reset -q --hard "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO" "$APP"
fi
ok "Код: $(git -C "$APP" log -1 --format='%h %s')"

say "Ставлю зависимости"
[ -d "$ROOT/venv" ] || python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install -q --upgrade pip
"$ROOT/venv/bin/pip" install -q -r "$BOT/requirements.txt"
ok "Зависимости установлены"

ENV="$BOT/.env"
if [ ! -f "$ENV" ] || [ "${1:-}" = "--reconfigure" ]; then
  say "Настройка. Ответь на несколько вопросов (Enter — пропустить необязательное)"
  echo "Токен бота из @BotFather. При вводе символы не видны — это нормально."
  read -r -s -p "BOT_TOKEN: " TOKEN </dev/tty; echo
  [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || fail "Похоже, токен введён неверно. Запусти скрипт ещё раз."
  ADMIN=$(ask "Твой Telegram ID (цифры, узнать у @userinfobot): ")
  CHANNEL=$(ask "ID канала «Точка сборки» (вида -100..., можно позже): ")
  MINIAPP=$(ask "Ссылка на Барометр (https://..., можно позже): ")

  cat >"$ENV" <<EOF
BOT_TOKEN=$TOKEN
ADMIN_IDS=$ADMIN
PAYMENT_MODE=stars
PROVIDER_TOKEN=
YOOKASSA_RECEIPT=1
CHANNEL_ID=$CHANNEL
MINIAPP_URL=$MINIAPP
TIMEZONE=Europe/Moscow
GRACE_HOURS=12
DB_PATH=bot.db
CONTENT_PATH=content.json
EOF
  ok "Настройки сохранены в $ENV"
fi
chown -R "$RUN_USER:$RUN_USER" "$ROOT"
chmod 600 "$ENV"   # токен читает только сам бот

say "Настраиваю автозапуск"
cat >/etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=Telegram bot «Точка сборки»
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$BOT
ExecStart=$ROOT/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"
sleep 4

if systemctl is-active -q "$SERVICE"; then
  ok "Бот запущен и будет сам подниматься после перезагрузки сервера"
  cat <<EOF

  Полезные команды:
    статус:      sudo systemctl status $SERVICE
    журнал:      sudo journalctl -u $SERVICE -f
    перезапуск:  sudo systemctl restart $SERVICE
    настройки:   sudo bash $BOT/deploy/install.sh --reconfigure
    обновить код и тексты из GitHub:  sudo bash $BOT/deploy/install.sh
EOF
else
  journalctl -u "$SERVICE" -n 30 --no-pager
  fail "Бот не запустился — пришли Claude последние строки выше (токен в них не попадает)"
fi
