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

Коды выхода: `2` — geoblock не «разрешено» (запись остановлена, это правильно); `3` — ошибка конфига (например, slug тега не найден → `discover`); `1` — упал компонент (Docker перезапустит).

## 5. OddsPapi на бесплатном тарифе (план — `docs/data_sources.md` §6)

Бюджет: 250 запросов в месяц и 40 в день. Счётчик хранится в `data/state/oddspapi_budget.json` и переживает перезапуски.

| День | Команда (`docker compose run --rm tools …`) | Запросов |
|---|---|---|
| 1 | `oddspapi-eval meta` | ~8 |
| 1, 4, 7 | `oddspapi-eval fixtures` | 3 каждый раз |
| 1, 4, 7 | `oddspapi-eval coverage --max-calls 10` | до 10 |
| 1 | `oddspapi-eval sample --per-group 8` | до 40 |
| 2–6, вечером | `oddspapi-eval burst --n-fixtures 2 --duration 180 --interval 5` (нужны матчи со стартом через 5–60 мин) | ~36 за запуск, не больше одного в день |
| 7 | `oddspapi-eval summary` (без запросов) | 0 |

Добавляйте `--out /app/data/reports/oddspapi_<шаг>.md`, чтобы сохранить вывод. Если OddsPapi даст trial с WS или большим лимитом, меняем `oddspapi.mode` в `config/recorder.yaml` на `paid_rest` или `ws` и перезапускаем рекордер.

## 6. Ежедневно

```bash
# cron (UTC): слить вчерашние мелкие файлы Parquet по часам
30 0 * * * cd /home/<user>/bot-polyt && docker compose run --rm tools compact --date $(date -u -d yesterday +\%F) >> data/compact.log 2>&1
```

- `df -h` — оценка 1–3 ГБ в сутки после сжатия (уточнить по факту).
- `docker compose ps` — `healthy`; `recorder_status.json` — растут `frames`, нет `dropped_rows`.
- По желанию: копия `data/raw` на другую машину (`rsync`) раз в сутки.

## 7. Через 7 дней

```bash
docker compose run --rm tools report --days 7 --out /app/data/reports/m1_data.md
docker compose run --rm tools oddspapi-eval summary --out /app/data/reports/oddspapi_summary.md
```

Пришлите `data/reports/*.md`. По ним я пишу вторую часть `docs/reports/M1.md` и заполняю таблицы в `docs/latency.md` и `docs/data_sources.md` §6. После этого — решение по тарифу OddsPapi и переход к стратегии.

## 8. Безопасность

- `.env` — только на сервере, права 600, в git не попадает. Рекордеру достаточно `ODDSPAPI_API_KEY`; ключи Polymarket не нужны до Live-S.
- Логи и Parquet маскируют всё, что похоже на ключ. Ключ OddsPapi не пишется даже в поле `endpoint`.
- Реальных ордеров рекордер отправить не может: в его коде нет вызовов ордеров (это проверяет тест `tests/test_architecture.py`).
