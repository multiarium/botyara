#!/data/data/com.termux/files/usr/bin/bash
# Запуск бота с автоперезапуском при падении
cd "$(dirname "$0")"
termux-wake-lock 2>/dev/null
while true; do
  echo "[$(date)] Запуск бота..."
  python bot.py
  echo "[$(date)] Бот упал, перезапуск через 10 сек..."
  sleep 10
done
