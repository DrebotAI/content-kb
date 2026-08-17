# Сетап

## Поточний стан
`bot.py` приймає IG-лінки, TikTok-лінки, переслані пости, картинки й голосові → аналіз через Codex → запис у Notion.
Один процес обслуговує кількох власників баз: хто написав — у ту базу й пише.

Вхідні дані:
- Instagram: пости, рилси, сторіз, профілі
- TikTok: відео
- Телеграм: переслані повідомлення, картинки, голосові, текст
- Відео без звуку: читаються кадри як картинки
- Карусельні пости і багатокартинкові повідомлення: усі слайди → OCR → один запис

## 1. Залежності

### Python пакети
```
pip install -r requirements.txt
```

### Бінарні утиліти (встановити окремо)

Код викликає ці CLI-утиліти напряму через `subprocess`. Переконайся, що вони в `$PATH`:

- **`ffmpeg`** — вилучення кадрів з німих відео та звукової доріжки
- **`codex`** — аналіз і транскрибування через Claude (встановлюється з `npm install -g @anthropic-ai/codex`)

yt-dlp уже в requirements.txt (через pip), не потрібен окремий інсталл.

## 2. `.env` — те, що спільне для всіх

### Обов'язкові
- `TELEGRAM_BOT_TOKEN` — токен від @BotFather
- `DEEPGRAM_API_KEY` — для транскрибування голосових
- Токени Notion: по одному на власника бази, іменуй як зручно (`NOTION_TOKEN`, `KENT_NOTION_TOKEN`),
  а в `tenants.json` посилайся на них через `env:`

### Опціональні
- `BATCH_DEBOUNCE_SECONDS` — як довго чекати на наступне повідомлення, перш ніж зшити пачку в один запис
  (за замовчуванням 25 сек; якщо 0, кожне повідомлення — окремий запис)
- `CODEX_BIN` — шлях до `codex`, якщо він не в `$PATH` (за замовчуванням `"codex"`)
- `CODEX_MODEL` — модель для аналізу постів (за замовчуванням `"gpt-5.6-sol"`)
- `CODEX_REASONING` — рівень міркування для Codex (за замовчуванням `"medium"`)
- `CODEX_TIMEOUT_SECONDS` — таймаут на один аналіз у секундах (за замовчуванням 300 сек)
- `IG_COOKIES_FILE` — файл кук для сторіз (опційно, для анонімних постів це не потрібно)
- `IG_USER_AGENT` — User-Agent для запитів до Instagram (опційно)
- `IG_PROXY_URL` — проксі для IG запитів (опційно)
- `IG_BROWSER_PROFILE` — теся для браузера ig_session_guardian (за замовчуванням `/home/tgsorter/ig-browser-profile`)
- `GPROXY_API_KEY` — ключ для автоматичної генерації проксі (опційно)
- `GPROXY_API_URL` — URL API для проксі (за замовчуванням `https://gproxy.net/api/v1/proxy/generate/`)
- `GPROXY_COUNTRY` — країна для проксі (за замовчуванням `"VN"`)
- `IG_USERNAME` — для ig_session_guardian автологіну (опційно)
- `IG_PASSWORD` — для ig_session_guardian автологіну (опційно)
- `TENANTS_FILE` — шлях до `tenants.json` (за замовчуванням поруч із кодом)
- `CONTEXT_FILE` — для старого однокористувацького режиму без `tenants.json` (за замовчуванням `"context.md"`)
- `ALLOWED_USER_ID`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`, `TENANT_NAME` — для старого режиму без tenants.json

## 3. `tenants.json` — хто в яку базу пише

Лежить поруч із кодом, у git не їде. Приклад — `tenants.example.json`:

```json
[
  {
    "name": "owner",
    "telegram_id": 111111111,
    "notion_token": "env:NOTION_TOKEN",
    "notion_database_id": "0123456789abcdef0123456789abcdef",
    "context_file": "context.md"
  },
  {
    "name": "kent",
    "telegram_id": 222222222,
    "notion_token": "env:KENT_NOTION_TOKEN",
    "notion_database_id": "https://www.notion.so/Knowledge-Base-0123…?v=…",
    "context_file": "context.kent.md"
  }
]
```

- `telegram_id` — числовий id. Хто не в списку — того бот повністю ігнорує.
  Дізнатись: написати боту `/id`, він відповість будь-кому.
- `notion_database_id` — можна вставляти URL бази цілком: id вийме сам
  (і не переплутає з id вʼю з `?v=`).
- `notion_token` — або значення, або `env:ІМʼЯ_ЗМІННОЇ` з `.env`.
- `context_file` — профіль власника для оцінки цінності. **У кожного свій.**
  Файла немає — бот оцінює без контексту й ставить здебільшого 📎, а не міряє
  чужий контент твоїми деалами. Шаблон — `context.example.md`.

Файла `tenants.json` немає взагалі — конфіг збирається з `.env`
(`ALLOWED_USER_ID` + `NOTION_TOKEN` + `NOTION_DATABASE_ID`), як було раніше.

## 4. Notion Knowledge Base — для кожного власника окремо

Робить сам власник, у своєму воркспейсі:

1. https://app.notion.com/developers/connections → в сайдбарі **Internal
   connections** → **Create a new connection** → назва + свій воркспейс.
   Створити може лише **Workspace Owner** воркспейса.
2. Вкладка **Configuration** → скопіювати **Installation access token** (`ntn_…`).
   Capabilities: Read, Update, Insert content.
3. Створити порожню сторінку, назвати «Knowledge Base».
4. На сторінці: **•••** (правий верхній кут) → **Connections** →
   **+ Add connection** → вибрати свою інтеграцію → підтвердити.
5. Прислати мені: токен, лінк на сторінку і свій Telegram ID (`/id` боту).

Notion перейменував integrations → connections; стара адреса
`notion.so/my-integrations` ще редіректить, але UI там уже інший.

Далі я:
```
python setup_notion.py <лінк на сторінку> env:KENT_NOTION_TOKEN
```
— створює базу з готовою схемою і друкує блок для `tenants.json`.

**Якщо база вже створена вручну**: `setup_notion.py` не потрібен, впиши id у
`tenants.json`. Але колонки доводиться додавати через нову версію API: бази,
створені в Notion після 2025-09-03, тримають схему в *data source*, і
`PATCH /v1/databases/<id>` зі старою версією API мовчки нічого не змінює
(200 OK, схема та сама).

Клієнт запінено на `Notion-Version: 2022-06-28` (`notion_store.py`,
`setup_notion.py`) — створення сторінок із `parent: database_id` на ній
працює й перевірене живим записом.

## 5. Перевірка перед тим, як віддавати бота людині

```
python doctor.py                # усі тенанти
python doctor.py kent --probe   # + створити тестову сторінку і заархівувати
```
Ловить рівно те, на чому спотикається кожен новий власник: не той токен,
не додана інтеграція в Connections, брак колонок у базі, мертва Instagram-сесія.

## 6. Міграція баз під нову схему (Content Potential + Hook)

Якщо база створена старою версією коду, потрібна міграція:

```
python backfill.py --schema          # додати нові колонки (безпечно повторювати)
python backfill.py --limit 5         # тестово на 5 записах, подивись очима
python backfill.py                   # переаналізувати все решта й заповнити нові поля
```

Ідемпотентно: сторінка, де Content Potential уже стоїть, пропускається;
заповнені вручну поля не перезаписуються.

## 7. Кодекс (Codex CLI)

На сервері має бути залогінений `codex` (`codex login`). Перед першим запуском
звірити `codex --help` — `ai_engine.py` викликає `codex exec` у неінтерактивному
режимі.

## 8. Instagram-сесія: автопідтримка

Якщо сторіз скачуються регулярно, сесія протухає за кілька днів. Альтернатива ручному
оновленню кук — запустити guardian як systemd timer:

```bash
sudo cp deploy/ig-session-guardian.service deploy/ig-session-guardian.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ig-session-guardian.timer
```

Служба `ig_session_guardian.py`:
- Відкриває браузер, заходить у Instagram через UI (навіть якщо потрібна 2FA)
- Витягує куки й записує в `IG_COOKIES_FILE`
- Може автоматично ротувати проксі через GProxy (якщо `GPROXY_API_KEY` заданий)
- Запускається таймером, не потрібна постійно

Це опціональна служба — без неї сторіз не скачуються, але решта функціоналу
(пости, рилси, TikTok) працює анонімно.

## 9. Режими бота

- Переслав IG/TikTok-лінк / пост / картинку / голосове → запис у свою базу + картка у відповідь
- `/voice`, потім пачка голосових → тільки транскрипти, без бази (вимикається після 60 с тиші; режим — на чат, не на всіх)
- Пачка текстів у ряд → один запис за `BATCH_DEBOUNCE_SECONDS` (за замовчуванням 25 сек тиші), зшиває через Codex
- Карусель або багатокартинкове повідомлення → OCR усіх слайдів → один запис
- Відео без звуку → витяги кадри → читаються як картинки
- `/id` → числовий Telegram ID; відповідає будь-кому

## 10. Запуск

Тест:
```
python bot.py
```
Постійна робота (systemd) — юніт лежить у `deploy/`. Під свій хост поправ у ньому
`User=` і `WorkingDirectory=`:
```bash
sudo cp deploy/tg-sorter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-sorter
```

## 11. Тести

```
pytest
```
105 тестів, мережу не чіпають — ганяються локально й на сервері перед рестартом.
