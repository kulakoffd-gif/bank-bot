# Bank notify bot — GitHub Actions edition

Мониторинг входящих платежей в Беларусбанк (icb.asb.by) через Playwright + Telegram уведомления.

## Что делает

Каждые 15 минут с 8:00 до 17:00 МСК (пн–пт) GitHub Actions запускает скрипт, который:

1. Опрашивает Telegram на новые команды от админа и выполняет их (`/pause`, `/resume`, `/check`, `/status`, `/last`, `/help`)
2. Если бот не на паузе — заходит на icb.asb.by, скачивает выписку, находит новые поступления
3. Шлёт уведомления в Telegram о каждом новом платеже
4. Сохраняет состояние (`state.json`) обратно в репо

## Управление через Telegram

| Команда | Что делает |
|---------|------------|
| `/help` | Справка по командам |
| `/status` | Статус и расписание |
| `/check` | Внеплановая проверка |
| `/last` | Последние ID учтённых поступлений |
| `/pause` | Поставить на паузу |
| `/resume` | Возобновить |

Команды обрабатываются при следующем запланированном запуске (до 15 минут).

## Настройка

Нужно создать **GitHub Secrets** (Settings → Secrets and variables → Actions):

| Secret | Что туда |
|--------|----------|
| `TELEGRAM_BOT_TOKEN` | токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | твой chat_id |
| `BANK_LOGIN` | логин от icb.asb.by |
| `BANK_PASSWORD` | пароль от icb.asb.by |
| `BANK_ACCOUNT_IBAN` | IBAN счёта для отслеживания |

## Запуск вручную

GitHub → вкладка Actions → "Bank check" → кнопка "Run workflow".
