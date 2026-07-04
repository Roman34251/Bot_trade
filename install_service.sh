#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Встановлення systemd-сервісу Bot_trade (запускати НА СЕРВЕРІ):
#
#   cd ~/Bot_trade && bash deploy/install_service.sh
#
# Після цього бот:
#   - стартує сам після ребута Oracle
#   - перезапускається сам після будь-якого падіння (через 10с)
# ═══════════════════════════════════════════════════════════════
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$DIR/.." && pwd)"
PY="$(command -v python3.11 || command -v python3)"
echo "Python:  $PY"
echo "Проєкт:  $PROJECT"
echo "Юзер:    $(whoami)"

# Захист від ДВОХ ботів одночасно (дубль-ордери!)
if pgrep -f "telegrambot.py" >/dev/null 2>&1; then
  echo ""
  echo "⚠️  Уже працює запущений вручну бот — спочатку зупини його:"
  pgrep -af "telegrambot.py"
  echo "   kill <PID>   (або Ctrl+C у його терміналі), потім запусти скрипт знову."
  exit 1
fi

sed -e "s|/usr/bin/python3.11|$PY|g" \
    -e "s|/home/ubuntu/Bot_trade|$PROJECT|g" \
    -e "s|User=ubuntu|User=$(whoami)|" \
    "$DIR/bot_trade.service" | sudo tee /etc/systemd/system/bot_trade.service >/dev/null

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
