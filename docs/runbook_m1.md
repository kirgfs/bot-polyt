# Runbook M1: рекордер на VPS в Ереване

Цель: 7 дней непрерывной записи книг спортивных рынков, Sports WS и OddsPapi (free/trial), потом отчёт (`docs/plan.md`, M1). Рекордер **ничего не торгует**: ключи Polymarket ему не нужны.

## 1. Сервер

- 2 vCPU, 4 ГБ RAM, от 100 ГБ диска; Ubuntu 24.04 LTS или аналог; часовой пояс UTC.
- Юрисдикция — Армения (решение 1). VPN, прокси и туннели для смены региона **не используем** (ToS 2.1.4).
- Нужны Docker Engine с плагином compose, `git`, `chrony`:

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git chrony
# Docker — по официальной инструкции для вашей ОС: https://docs.docker.com/engine/install/
sudo systemctl enable --now chrony
chronyc tracking            # "System time" должно быть в пределах единиц миллисекунд
timedatectl set-timezone UTC
```

Входящие порты: только SSH. Исходящие: 443 открыт.

## 2. Установка

```bash
git clone <repo> bot-polyt && cd bot-polyt
cp .env.example .env && chmod 600 .env
# ODDSPAPI_API_KEY=<ключ free/trial> — только если будете запускать проверку OddsPapi
mkdir -p data && sudo chown -R 10001:10001 data   # пользователь контейнера — uid 10001
docker compose build
```

## 3. Проверки до записи (результаты — в `docs/latency.md`, пришлите мне вывод)

```bash
docker compose run --rm tools geocheck                     # должно быть "allowed: country=AM"
docker compose run --rm tools netcheck --out /app/data/reports/netcheck.md
docker compose run --rm tools latency  --out /app/data/reports/latency.md
docker compose run --rm tools discover --out /app/data/reports/discover.md
```

- `geocheck` вернул не `allowed` → **стоп**. Ничего не обходим; выбираем другой хостинг в разрешённой юрисдикции.
- `netcheck` показал вердикт не `ok` → смотрим заметки. DNS-подмена, обрыв TLS или страница блокировки → другой провайдер.
- `discover` показывает, какие `sportsMarketType` и теги реально есть у футбола и баскетбола. Если в `config/recorder.yaml` стоит не то значение, правим `market_types` и пересобирать не нужно (конфиг монтируется). `require_tag_slugs` включаем только если тег есть у **всех** матчей вида спорта.

## 4. Запуск записи

```bash
docker compose up -d recorder
docker compose logs -f --tail=100 recorder     # geoblock_status, gamma_tags_resolved, market_pool_updated
docker compose ps                              # через ~3 мин статус healthy
cat data/state/recorder_status.json            # счётчики: активы, соединения, рассинхроны, строки
```

Коды выхода: `2` — geoblock не «разрешено» (запись остановлена, это правильно); `3` — ошибка конфига (например, slug тега не найден → `discover`); `1` — упал компонент (Docker перезапустит); `137` — контейнер упёрся в `mem_limit` (Docker перезапустит, см. ниже).

### Память

Рекордер в работе занимает ~150–200 МБ (стенд `make soak`: 3000 активов, 10 минут, пик anon RSS 203 МБ, без роста). Потолок контейнера — `RECORDER_MEM_LIMIT` в `.env` (по умолчанию 400m); сумма с `TOOLS_MEM_LIMIT` должна быть меньше RAM сервера минус ~300 МБ.

```bash
docker stats --no-stream                                    # MEM USAGE / LIMIT
docker compose logs --since 10m recorder | grep recorder_memory | tail -3
python3 -c "import json; print(json.load(open('data/state/recorder_status.json'))['memory'])"
```

- `recorder_memory` — раз в минуту: `rss_mb`, `anon_mb` (то, что OOM-killer называет anon-rss), `peak_mb`, число активов, буфер Parquet. Выше `health.rss_warn_mb` (250) — `recorder_memory_high`.
- Памяти мало → уменьшить `discovery.max_subscribed_markets` в `config/recorder.yaml` (1500 по умолчанию, ~20 КБ на рынок) и `docker compose restart recorder`. При срезании в логе `subscribed_markets_capped`.

### Обновление рекордера (после OOM 2026-09-25)

```bash
cd /root/polybot && git pull
docker compose build
docker compose up -d recorder          # пересоздаст контейнер с новым образом и mem_limit
docker compose logs -f --tail=50 recorder | grep -E "recorder_memory|market_pool_updated|error"
```

## 5. OddsPapi на бесплатном тарифе (план — `docs/data_sources.md` §6)

Платный тариф не берём (решение 7). Бюджет: 250 запросов в месяц и 40 в день. Счётчик хранится в `data/state/oddspapi_budget.json` и переживает перезапуски.

| Когда | Команда (`docker compose run --rm tools …`) | Запросов |
|---|---|---|
| День 1 | `oddspapi-eval meta` | ~8 |
| Дни 1, 4, 7 (дальше — раз в 3 дня) | `oddspapi-eval fixtures` | 3 каждый раз |
| **Каждый день** (cron, ниже) | `oddspapi-eval coverage --max-calls 3` — контроль: цены Pinnacle по турнирам, где есть рынки Polymarket | до 3 |
| День 1 | `oddspapi-eval sample --per-group 5` | до 25 |
| 2 вечера за неделю | `oddspapi-eval burst --n-fixtures 2 --duration 180 --interval 5` (нужны матчи со стартом через 5–60 мин) | ~36 за запуск |
| День 7 | `oddspapi-eval summary` (без запросов) | 0 |

Итого за месяц примерно 225 из 250. Ежедневный контроль даёт главный вход для решения 7: разрыв цен Polymarket и Pinnacle (раздел «Цена Polymarket против Pinnacle» в `polybot report`).

```bash
# cron (UTC): ежедневный контроль Pinnacle
0 12 * * * cd /root/polybot && docker compose run --rm tools oddspapi-eval coverage --max-calls 3 --out /app/data/reports/oddspapi_control_$(date -u +\%F).md >> data/oddspapi.log 2>&1
```

Добавляйте `--out /app/data/reports/oddspapi_<шаг>.md`, чтобы сохранить вывод.

## 6. Ежедневно: отчёт за сутки, очистка диска, отправка в репозиторий

Каждую ночь `polybot daily`:
1. сливает мелкие файлы Parquet за вчера по часам;
2. собирает отчёт за каждые завершённые сутки UTC без отчёта: `data/reports/daily/m1_<дата>.md` — те же 12 разделов, что и недельный, плюс здоровье рекордера (память, перезапуски, потерянные строки);
3. удаляет сырые данные старше `daily.keep_raw_days` полных суток (2 по умолчанию, `config/recorder.yaml`). Удаляются только сутки, отчёт за которые уже собран. Сегодняшние данные и `oddspapi_rest` (крошечный, нужен для сопоставления) не удаляются никогда. Сутки с упавшим отчётом остаются и пересобираются следующей ночью.

Затем `scripts/publish_reports.sh` переносит все `data/reports/**/*.md` и текущий `recorder_status.json` в отдельный приватный репозиторий. Локальные копии удаляются только после успешного `git push`; при сбое отчёты уйдут в следующий раз. Отчёт считается в DuckDB с лимитом `daily.duckdb_memory_mb` (128 МБ, 1 поток; лишнее — на диск в `data/tmp`), контейнер ограничен `TOOLS_MEM_LIMIT`. Замер на синтетических сутках в 4,9 млн строк (тяжелее ожидаемых реальных): весь `daily` — пик 419 МБ, ~3 мин (компактизация — 165 МБ, отчёт — 362 МБ).

```bash
# cron (UTC); заменяет прежнюю строку с compact
20 0 * * * cd /root/polybot && docker compose run --rm tools daily >> data/daily.log 2>&1; /root/polybot/scripts/publish_reports.sh >> data/publish.log 2>&1
```

На диске остаётся ~3 суток сырых данных (сегодня и 2 полных). Если понадобятся данные для бэктеста этапа 0 (M3), поднимите `keep_raw_days` или скачивайте `data/raw/date=<дата>` до удаления. Вручную: `docker compose run --rm tools daily --date 2026-09-26` пересобирает отчёт за день, `--no-delete` отключает очистку.

### Репозиторий для отчётов (один раз)

Отдельный **приватный** репозиторий, а не ветка в репозитории с кодом: ключ на VPS получает право записи только туда и не может изменить код бота.

1. На GitHub создайте пустой приватный репозиторий, например `polybot-reports`.
2. На VPS создайте ключ только для него:
   ```bash
   ssh-keygen -t ed25519 -N "" -C "polybot-vps-reports" -f /root/.ssh/polybot_reports
   cat /root/.ssh/polybot_reports.pub
   ```
3. GitHub → `polybot-reports` → Settings → Deploy keys → Add deploy key: вставьте ключ, включите **Allow write access**.
4. На VPS:
   ```bash
   git clone -c core.sshCommand="ssh -i /root/.ssh/polybot_reports -o IdentitiesOnly=yes" \
       git@github.com:<ваш-логин>/polybot-reports.git /root/polybot-reports
   cd /root/polybot-reports
   git config user.name "polybot-vps" && git config user.email "polybot-vps@users.noreply.github.com"
   echo "# Отчёты рекордера с VPS" > README.md && git add README.md && git commit -m "init" && git push -u origin HEAD
   /root/polybot/scripts/publish_reports.sh     # первая отправка: должно быть "published N report(s)"
   ```
5. Чтобы я читал отчёты сам, добавьте `polybot-reports` в доступ GitHub-приложения Claude (то же, что для `bot-polyt`).

Проверки раз в день:
- `df -h` — оценка 1–3 ГБ в сутки после сжатия (уточнить по факту);
- `docker compose ps` — `healthy`; `recorder_status.json` — растут `frames`, нет `dropped_rows`, `memory.anon_mb` не растёт изо дня в день;
- `tail data/daily.log data/publish.log` — нет `ОШИБКА`.

## 7. Через 7 дней

```bash
docker compose run --rm tools oddspapi-eval summary --out /app/data/reports/oddspapi_summary.md
```

Напишите мне: я прочитаю 7 ежедневных отчётов из репозитория и напишу вторую часть `docs/reports/M1.md`, заполню таблицы в `docs/latency.md` и `docs/data_sources.md` §6. После этого — вывод, жизнеспособен ли этап 0 без платных данных (решение 7), и переход к стратегии. `polybot report --days 7` (отчёт одним файлом за неделю) работает, только если сырые данные за неделю ещё на диске (`keep_raw_days: 7`).

## 8. Безопасность

- `.env` — только на сервере, права 600, в git не попадает. Рекордеру достаточно `ODDSPAPI_API_KEY`; ключи Polymarket не нужны до Live-S.
- Логи и Parquet маскируют всё, что похоже на ключ. Ключ OddsPapi не пишется даже в поле `endpoint`.
- Реальных ордеров рекордер отправить не может: в его коде нет вызовов ордеров (это проверяет тест `tests/test_architecture.py`).

## 9. Бумажный мини-бот: футбол (решение 9)

Мини-бот котирует на бумаге: книги Polymarket живые, ордера выдуманные. Ключи Polymarket и деньги не нужны; реальный ордер он отправить не может (правило 1, `docs/architecture.md` §13). Лиги: Серия А, Ла Лига, Лига 1, Эредивизи. Все команды — в папке проекта на VPS.

### 9.1 Обновление

```bash
git pull && docker compose build
```

### 9.2 Telegram (один раз)

1. Токен бота и ваш id — только в `.env` на сервере (не в репозиторий, не в чаты):
   ```
   TELEGRAM_BOT_TOKEN=<токен от @BotFather>
   TELEGRAM_ALLOWED_CHAT_IDS=<ваш user id>
   ```
   Токен уже побывал в переписке — перевыпустите его в @BotFather и вставьте новый.
2. Откройте чат со своим ботом в Telegram и нажмите **Start**. Без этого Telegram отвечает 403 и сообщения не доходят (в логе `telegram_send_rejected` с подсказкой).

### 9.3 Проверка конфигурации: `--dry-run`

Slug-и тегов лиг и тип рынка футбола — догадки: из облака Gamma не видна (`docs/api_notes.md` §11, чек-лист п. 19 и 22).

```bash
docker compose run --rm tools minibot --dry-run --out /app/data/reports/minibot/dry_run.md
```

- **«Лиги»:** у каждого slug есть tag id и открытые матчи. «не найден» → правильный slug есть в таблице «Теги футбольных матчей»; исправьте `leagues` в `config/minibot.yaml` (пересборка не нужна: конфиг монтируется).
- **«Типы рынков»:** у рынков «победа / ничья» `sportsMarketType` = `moneyline`, исходы `Yes / No`. Иначе поправьте `market_types`.
- **«Отбор сейчас»:** сколько рынков выбрано и есть ли у них награды.
- Пришлите мне вывод: я сверю slug-и, типы рынков и шаблоны правил.

### 9.4 Запуск

Память: мини-бот занимает десятки МБ (потолок `MINIBOT_MEM_LIMIT`, 300m). Решение 9 ставит мини-бот раньше отчёта M1, поэтому рекордер можно остановить. Если RAM сервера 2 ГБ и больше, рекордер можно не трогать: сумма `RECORDER_MEM_LIMIT` + `MINIBOT_MEM_LIMIT` + `TOOLS_MEM_LIMIT` должна быть меньше RAM минус ~300 МБ.

```bash
docker compose stop recorder                    # при нехватке памяти
docker compose up -d minibot
docker compose logs -f --tail=100 minibot       # minibot_start, minibot_selection, minibot_market_added
docker compose ps                               # minibot: healthy через ~3 мин
docker compose run --rm tools minibot-status --send   # статус сейчас, копия — в Telegram
```

Коды выхода — как у рекордера: `2` geoblock, `3` конфиг (например, slug лиги не найден — снова `--dry-run`), `1` сбой (Docker перезапустит). При отказе старта в Telegram приходит «Мини-бот не стартует» (не чаще раза в 6 ч).

### 9.5 Что приходит в Telegram

- «Мини-бот запущен» / «остановлен» — при каждом старте и штатной остановке;
- **итоги дня** — после 00:00 UTC (04:00 по Еревану), без звука: счёт, P&L за день и с начала, сделки, оборот, оценка ребейтов и наград, рынки;
- **статус** — в 00:00, 06:00, 12:00 и 18:00 по Еревану, без звука: котировки по рынкам, позиции, время старта;
- **тревоги** — дневной лимит убытка (котировки сняты до конца суток UTC), geoblock, расчёт рынка после матча.

### 9.6 Файлы и ежедневный распорядок

- `data/reports/minibot/paper_<день>.md` — итоги суток. Уходят в приватный репозиторий вместе с отчётами рекордера (`scripts/publish_reports.sh`, §6).
- `data/reports/minibot/rules_templates.md` — шаблоны правил резолюции (правило 4). Прочитайте; одобренные id впишите в `rules.approved_templates` в `config/minibot.yaml`, затем `docker compose restart minibot`. Пока шаблон не одобрен, рынок котируется только на бумаге и помечается ⚠️.
- `data/state/minibot_state.json` — бумажный портфель: переживает перезапуск. Начать заново с новым депозитом: остановить мини-бот, удалить файл, запустить.
- `data/minibot/raw/` — сырые данные мини-бота. Мини-бот сам сжимает их и удаляет книги старше `keep_raw_days` суток; бумажные ордера и сделки хранятся всегда.
- Параметры (депозит, лимиты, число рынков, спред) — `config/minibot.yaml`, затем `docker compose restart minibot`. Депозит из конфига действует только для нового портфеля.

### 9.7 Через неделю

Пришлите ссылку на репозиторий с отчётами (или файлы `paper_*.md`). По ним решаем: оставить параметры, поменять их, вернуться к рекордеру для отчёта M1 — или обсуждать реальный депозит. Реальные ордера — только отдельным решением и с явным подтверждением в чате (правило 1).

