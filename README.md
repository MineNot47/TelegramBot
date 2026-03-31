# TelegramBot (aiogram v3 + SQLite)

## Запуск локально

1. Установить зависимости:
   - `pip install -r requirements.txt`
2. Задать переменные окружения:
   - `BOT_TOKEN` — токен бота
   - `FLYER_API_KEY` — ключ Flyer (опционально)
   - `BOT_PROXY` — прокси (опционально)
3. Запуск:
   - `python bot.py`

## Деплой на Railway (Polling)

1. Залей проект в GitHub (убедись, что `bot.sqlite3` не коммитится — он в `.gitignore`).
2. Railway → **New Project** → **Deploy from GitHub repo**.
3. Variables (ENV):
   - `BOT_TOKEN`
   - `FLYER_API_KEY` (если используешь Flyer)
   - `SUPPORT_USERNAME` (опционально)
   - `DEBUG_LOG=1` (опционально)
4. Для сохранения данных SQLite между перезапусками:
   - добавь Volume, например `/data`
   - поставь `DB_PATH=/data/bot.sqlite3`
5. Start Command:
   - `python bot.py`

## Вебхук Flyer (опционально)

Нужен только если ты используешь вебхуки Flyer.

1. Variables:
   - `FLYER_WEBHOOK_SECRET` — любой длинный секрет
   - Railway автоматически задаёт `PORT` (используется ботом)
2. В панели Flyer укажи URL:
   - `https://<railway-domain>/flyer/webhook/<FLYER_WEBHOOK_SECRET>`

