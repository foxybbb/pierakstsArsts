# Мониторинг записи к врачу (eveselibaspunkts.lv → Telegram)

По расписанию проверяет ближайшее свободное время (valsts apmaksāts / maksas) у выбранных врачей
и шлёт сообщение в Telegram, когда время появляется или освобождается более раннее.

## Запуск
1. Создайте бота у @BotFather, напишите ему любое сообщение, узнайте chat_id:
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → `message.chat.id`.
2. `cp .env.example .env` и заполните токен и chat_id.
3. Врачей и время проверок настройте в `config/config.yaml` (правки подхватываются без рестарта).
4. `docker compose up -d --build`, логи: `docker compose logs -f`.

## Как это работает
Страница — Vue-приложение; данные приходят из `POST /lv/Booking/ListSpecialistCalendars`
(нужен Laravel CSRF: cookie `XSRF-TOKEN` → заголовок `X-XSRF-TOKEN`). Скрипт делает это обычным HTTP;
если не получилось (например, Cloudflare), автоматически использует headless Chromium (Playwright).

## Команды бота
`/check` — проверить сейчас, `/status`, `/history` — в какие часы/дни реально меняется база, `/schedule`.

## Когда обновляется база
Клиники не публикуют время открытия новых слотов. RAKUS записывает в пределах скользящего окна
~6 месяцев, т.е. новые государственные слоты открываются постепенно; отменённые записи
освобождаются в любой момент. Поэтому: первую неделю поставьте `interval_minutes: 30`,
затем посмотрите `/history` и оставьте в `check_times` только «горячие» часы.

Данные: `data/state.json` (последнее состояние), `data/history.jsonl` (все изменения с временем).
