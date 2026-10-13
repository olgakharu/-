#!/usr/bin/env bash
# Поднимает нового Telegram-агента по схеме «мост» (/home/ubuntu/agent-runtime),
# клонируя авторизацию главного агента. Та же подписка Claude — повторный вход
# не нужен. У каждого агента свой бот, своя папка ИМЯ-home, свой runtime
# (история разговора, логи) и свои строки в cron. Общий код моста не меняется.
# Использование: new-agent.sh <ИМЯ> <ТОКЕН_БОТА> <TG_ID> [МОДЕЛЬ]
set -euo pipefail

NAME="${1:?usage: new-agent.sh <ИМЯ> <ТОКЕН> <TG_ID> [МОДЕЛЬ]}"
TOKEN="${2:?нужен токен бота}"
TGID="${3:?нужен Telegram ID}"
MODEL="${4:-}"

USERHOME="/home/ubuntu"
MAIN="Kharuassistant"
BASE="$USERHOME/${MAIN}-home"
RT="$USERHOME/agent-runtime"
NEW="$USERHOME/${NAME}-home"
BOT="$RT/telegram-bot.py"

# ── Проверки ───────────────────────────────────────────────────────────────
[ "$(id -un)" = "ubuntu" ] || { echo "Запускай от пользователя ubuntu"; exit 1; }
case "$HOME" in "$BASE"|"$USERHOME") ;; *)
  echo "Создавать агентов может только главный агент $MAIN"; exit 1;; esac
case "$NAME" in ''|*[!A-Za-z0-9_-]*) echo "ИМЯ: только латиница, цифры, - и _"; exit 1;; esac
case "$TGID" in ''|*[!0-9]*) echo "TG_ID должен быть числом"; exit 1;; esac
case "$MODEL" in *[!A-Za-z0-9._-]*) echo "Странное имя модели: $MODEL"; exit 1;; esac

[ -f "$BASE/.claude/.credentials.json" ] || { echo "Главный агент не залогинен: нет $BASE/.claude/.credentials.json"; exit 1; }
ls -d "$BASE"/.local/share/claude/versions/* >/dev/null 2>&1 || { echo "Нет программы claude в $BASE/.local/share/claude"; exit 1; }
[ -x "$RT/keepalive.sh" ] && [ -f "$BOT" ] || { echo "Нет моста в $RT"; exit 1; }

# Сторож ищет процесс по началу строки «telegram-bot.py ИМЯ», поэтому имена,
# где одно — начало другого (NOVA и NOVA2), перепутаются. Такие запрещаем.
for d in "$USERHOME"/*-home; do
  [ -d "$d" ] || continue
  other="$(basename "$d")"; other="${other%-home}"
  case "$other" in "$NAME"*) echo "Имя конфликтует с агентом '$other' — выбери другое"; exit 1;; esac
  case "$NAME" in "$other"*) echo "Имя конфликтует с агентом '$other' — выбери другое"; exit 1;; esac
done

# Один бот = один агент: два моста на одном токене мешают друг другу.
if grep -qsxF "TELEGRAM_BOT_TOKEN=$TOKEN" "$USERHOME"/*-home/.claude/channels/telegram/.env; then
  echo "Этот токен уже занят другим агентом — нужен новый бот от @BotFather"; exit 1
fi

UNAME="$(curl -s --max-time 15 "https://api.telegram.org/bot${TOKEN}/getMe" | grep -o '"username":"[^"]*"' | cut -d'"' -f4 || true)"
[ -n "$UNAME" ] || { echo "Токен бота недействителен (getMe не прошёл)"; exit 1; }

# ── Клонирование ───────────────────────────────────────────────────────────
# Берём авторизацию, настройки и программу claude. НЕ берём историю разговоров,
# память и личные инструкции главного агента, его Telegram-настройки и runtime.
echo "Клонирую $MAIN -> $NAME (бот @$UNAME, доступ $TGID, модель ${MODEL:-по умолчанию})"
mkdir -m 700 "$NEW"
tar -C "$BASE" -cf - \
  --exclude='.claude/projects' \
  --exclude='.claude/todos' \
  --exclude='.claude/shell-snapshots' \
  --exclude='.claude/file-history' \
  --exclude='.claude/session-env' \
  --exclude='.claude/debug' \
  --exclude='.claude/history.jsonl' \
  --exclude='.claude/CLAUDE.md' \
  --exclude='.claude/channels/telegram' \
  .claude .local/share/claude | tar -C "$NEW" -xf -
[ -f "$BASE/.claude.json" ] && cp -a "$BASE/.claude.json" "$NEW/.claude.json"

TG="$NEW/.claude/channels/telegram"
mkdir -p "$TG/inbox" "$NEW/runtime"
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN" > "$TG/.env"
chmod 600 "$TG/.env"
cat > "$TG/access.json" <<JSON
{
  "dmPolicy": "allowlist",
  "allowFrom": ["$TGID"]
}
JSON
chmod 600 "$TG/access.json"

# ── Cron: сторож моста и смена сессии, как у главного агента ───────────────
ENVP="${MODEL:+MODEL=$MODEL }"
CLOG="$NEW/runtime/cron.log"
crontab -l > "$NEW/runtime/crontab.before" 2>/dev/null || true
{
  cat "$NEW/runtime/crontab.before"
  echo "# агент $NAME (фабрика, $(date +%F))"
  echo "* * * * * ${ENVP}$RT/keepalive.sh $NAME >> $CLOG 2>&1"
  echo "@reboot ${ENVP}$RT/keepalive.sh $NAME >> $CLOG 2>&1"
  echo "*/10 * * * * ${ENVP}$RT/rotate.sh $NAME >> $CLOG 2>&1"
} | crontab -

# ── Запуск: мост поднимет cron (в течение минуты), здесь только ждём ───────
echo "Жду, пока сторож поднимет мост (до 90 сек)..."
for _ in $(seq 1 18); do
  if pgrep -f "python3 $BOT $NAME" >/dev/null 2>&1; then
    echo "OK: агент '$NAME' запущен. бот=@$UNAME папка=$NEW"
    echo "Попроси владельца отправить /start боту @$UNAME."
    exit 0
  fi
  sleep 5
done
echo "ВНИМАНИЕ: мост '$NAME' не поднялся. Смотри:"
echo "  tail $NEW/runtime/keepalive.log $NEW/runtime/bot.out $CLOG"
exit 1
