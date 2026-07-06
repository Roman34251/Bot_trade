#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Встановлення systemd-сервісу Bot_trade (запускати НА СЕРВЕРІ Oracle):
#
#   cd ~/Bot_trade
#   sed -i 's/\r$//' install_service.sh bot_trade.service   # чистка Windows-символів
#   bash install_service.sh
#
# Працює незалежно від того, лежить скрипт у корені проєкту чи в deploy/.
# Після цього бот:
#   - стартує сам після ребута Oracle
#   - перезапускається сам після будь-якого падіння (через 10с)
# ═══════════════════════════════════════════════════════════════
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

# Знаходимо КОРІНЬ проєкту (де лежить telegrambot.py): поруч зі скриптом
# або на рівень вище (якщо скрипт у deploy/).
if [ -f "$DIR/telegrambot.py" ]; then
  PROJECT="$DIR"
elif [ -f "$DIR/../telegrambot.py" ]; then
  PROJECT="$(cd "$DIR/.." && pwd)"
else
  echo "❌ Не знайшов telegrambot.py біля скрипта."
  echo "   Поклади install_service.sh у корінь проєкту (~/Bot_trade) і запусти звідти."
  exit 1
fi

# Файл юніта — поруч зі скриптом (у корені або в deploy/)
SERVICE_SRC="$DIR/bot_trade.service"
if [ ! -f "$SERVICE_SRC" ]; then
  echo "❌ Немає bot_trade.service поруч зі скриптом ($DIR)."
  echo "   Поклади bot_trade.service у ту саму теку, що й install_service.sh."
  exit 1
fi

PY="$(command -v python3.11 || command -v python3)"
echo "Python:  $PY"
echo "Проєкт:  $PROJECT"
echo "Юнит:    $SERVICE_SRC"
echo "Юзер:    $(whoami)"

# Захист від ДВОХ ботів одночасно (дубль-ордери!)
if pgrep -f "telegrambot.py" >/dev/null 2>&1; then
  echo ""
  echo "⚠️  Уже працює запущений вручну бот — спочатку зупини його:"
  pgrep -af "telegrambot.py"
  echo "   kill <PID>   (або Ctrl+C у його терміналі), потім запусти скрипт знову."
  exit 1
fi

# Генеруємо юніт під реальні шляхи; tr -d '\r' чистить Windows-символи
sed -e "s|/usr/bin/python3.11|$PY|g" \
    -e "s|/home/ubuntu/Bot_trade|$PROJECT|g" \
    -e "s|User=ubuntu|User=$(whoami)|" \
    "$SERVICE_SRC" | tr -d '\r' | sudo tee /etc/systemd/system/bot_trade.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now bot_trade
sleep 3
sudo systemctl status bot_trade --no-pager -l | head -14

echo ""
echo "✅ Готово. Команди керування:"
echo "   sudo systemctl status bot_trade     # стан"
echo "   sudo journalctl -u bot_trade -f     # живі логи"
echo "   sudo systemctl restart bot_trade    # перезапуск (після заміни файлів)"
echo "   sudo systemctl stop bot_trade       # зупинити"
