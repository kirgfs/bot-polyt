# bot-polyt — Sports market-making для Polymarket

Бот для маркет-мейкинга на спортивных рынках Polymarket (CLOB V2):
- **прематч:** справедливая цена — консенсус коэффициентов острых букмекеров без маржи;
- **лайв-теннис:** Марковская модель по очкам.

Цель — стабильный неотрицательный markout и положительный CLV; капитал масштабируется только по гейтам.

## Статус

| Веха | Состояние |
|---|---|
| M0 — исследование, архитектура, план | ✅ готово: [`docs/reports/M0.md`](docs/reports/M0.md) |
| Решения пользователя | ✅ приняты 2026-09-25: [`docs/plan.md`](docs/plan.md#решения-пользователя-приняты-2026-09-25) |
| M1 — рекордер данных и замеры | 🟡 код готов ([`docs/reports/M1.md`](docs/reports/M1.md)); ждём 7 дней записи на VPS (Ереван) и отчёт |

Код стратегии пишется только после отчёта M1.

## Быстрый старт

Разработка (Python 3.12):
```bash
make install && make check        # ruff, mypy --strict, pytest (без сети)
```
VPS (Docker) — по [`docs/runbook_m1.md`](docs/runbook_m1.md):
```bash
docker compose build
docker compose run --rm tools geocheck    # старт только если Polymarket разрешён для IP сервера
docker compose up -d recorder
```
Ночью (cron): `docker compose run --rm tools daily` — отчёт за сутки и очистка старых сырых данных, затем `scripts/publish_reports.sh` — отчёты в отдельный приватный репозиторий (`docs/runbook_m1.md` §6). Память рекордера — ~150–200 МБ, проверка — `make soak`.

## Документы
- [`docs/api_notes.md`](docs/api_notes.md) — API Polymarket (CLOB V2, WS, комиссии, награды, спорт) с источниками
- [`docs/data_sources.md`](docs/data_sources.md) — коэффициенты и счёт: цены, задержки, выбор
- [`docs/architecture.md`](docs/architecture.md) — архитектура и отклонения от ТЗ
- [`docs/risks.md`](docs/risks.md) — риски и неизвестные
- [`docs/capacity.md`](docs/capacity.md) — ёмкость рынка и реалистичность цели
- [`docs/plan.md`](docs/plan.md) — вехи M1–M8, критерии приёмки, решения пользователя
- [`docs/latency.md`](docs/latency.md) — задержки и доступность VPS → Polymarket
- [`docs/runbook_m1.md`](docs/runbook_m1.md) — развёртывание рекордера и неделя записи
- [`CLAUDE.md`](CLAUDE.md) — правила и соглашения проекта

## Безопасность
- По умолчанию режим `paper`. Реальные ордера — только при `LIVE_TRADING=true` и явном подтверждении.
- Под бота — отдельный кошелёк с лимитом депозита. Секреты хранятся только в `.env` (см. [`.env.example`](.env.example)), он не коммитится.
- Бот работает только там, где Polymarket разрешён. При старте и раз в 10 минут он проверяет geoblock (`blocked == false` и страна из разрешённого списка) и не обходит ограничения: никаких VPN и прокси.
- Рекордер M1 ничего не торгует: ключи Polymarket ему не нужны.
