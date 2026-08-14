# Lux Express Price Monitor

Отслеживает цены на рейсы Lux Express и шлёт уведомления в Telegram при изменении.
Работает в GitHub Actions — бесплатно, в облаке, без необходимости держать компьютер включённым.

## ⚠️ Перед началом

Сайт luxexpress.eu запрещает автоматический сбор данных в своём `robots.txt` и защищён
Cloudflare. Это техническое решение, но не гарантия того, что оно соответствует условиям
использования сайта — имей это в виду.

## Шаг 1. Создать Telegram-бота

1. Напиши в Telegram боту **@BotFather**, команда `/newbot`, следуй инструкциям.
2. Получишь токен вида `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` — это `TELEGRAM_BOT_TOKEN`.
3. Напиши своему новому боту любое сообщение (например «привет»), чтобы он тебя увидел.
4. Открой в браузере:
   `https://api.telegram.org/bot<ТВОЙ_ТОКЕН>/getUpdates`
   Найди в ответе `"chat":{"id": ...}` — это `TELEGRAM_CHAT_ID`.

## Шаг 2. Выложить проект на GitHub

1. Создай новый **приватный** репозиторий на GitHub.
2. Залей туда все файлы из этой папки (`monitor.py`, `requirements.txt`, `.github/`, `README.md`).

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<твой-юзернейм>/<репозиторий>.git
git push -u origin main
```

## Шаг 3. Добавить секреты

В репозитории: **Settings → Secrets and variables → Actions → New repository secret**

- `TELEGRAM_BOT_TOKEN` — токен из шага 2
- `TELEGRAM_CHAT_ID` — chat_id из шага 2

## Шаг 4. Проверить

Во вкладке **Actions** репозитория найди workflow «Lux Express Price Monitor» →
**Run workflow** (кнопка справа) — запустит вручную, не дожидаясь расписания.
Проверь логи и Telegram.

После первого успешного запуска бот будет сам срабатывать каждые 30 минут
и писать в Telegram только когда цена реально изменилась.

## Настройка маршрутов

Список маршрутов задаётся в начале `monitor.py`, в списке `ROUTES`.
`from_stop_id` / `to_stop_id` — числа из URL страницы поиска на сайте
(`fromBusStopId` и `toBusStopId`).

## Изменить частоту проверки

В `.github/workflows/monitor.yml`, строка `cron: '0,30 * * * *'`.
Например, `*/15 * * * *` — каждые 15 минут. Учитывай, что GitHub Actions
даёт ограниченное бесплатное время выполнения в месяц (обычно этого более
чем достаточно для такой задачи).
