# Задержки и доступность: VPS (Ереван) → Polymarket

> Решение пользователя 1 (2026-09-25): VPS в Армении. Ожидание пользователя — ~70–100 мс до CLOB. Для прематча и окна ~3 с в лайве это приемлемо.
> Таблицы ниже заполняются **на VPS**. Из облачного окружения разработки хосты Polymarket закрыты (egress 403), поэтому здесь пока нет ни одного реального числа.

## 1. Что меряем и почему

Биржа (CLOB) стоит в AWS eu-west-2 (Лондон) за Cloudflare [2nd, `docs/api_notes.md` §15]. Отсюда три разных «задержки»:

| Метрика | Что показывает | Как меряем |
|---|---|---|
| TCP connect до `clob.polymarket.com` | RTT до ближайшей точки Cloudflare (edge), **не** до биржи | `polybot latency`: `open_connection` × 20 |
| REST `GET /time` на тёплом соединении | Запрос через edge до origin и обратно — ближе всего к «сколько идёт наш ордер» | `polybot latency` × 200; рекордер — раз в минуту всю неделю (`probe_rest`) |
| REST `GET /time` на новом соединении | TCP + TLS + запрос: цена переподключения | `polybot latency` × 20 |
| REST `GET /book` | Обычный запрос на чтение к бирже | `polybot latency` |
| WS market: PING→PONG | RTT прикладного уровня до WS-сервера | `polybot latency` (PING раз в 2 с) и рекордер (раз в 10 с, `clob_market_ws`, `kind=probe`) |
| WS market: `timestamp` сервера → наш приём | Одностороння задержка событий книги. **Зависит от синхронизации часов**: chrony у нас, NTP у биржи | `polybot latency`; рекордер — поле `server_ts_ms` каждого кадра |

Дополнительно:
- код дата-центра Cloudflare из заголовка `cf-ray` (например, `…-FRA`) показывает, через какой edge идёт трафик;
- разница наших часов с CLOB `/time` (точность ±1 с) ловит грубый дрейф;
- `chronyc tracking` на хосте показывает точное смещение.

Для лайва важна цепочка «событие → наша отмена подтверждена» (`docs/data_sources.md` §2). Сетевая часть этой цепочки — строки REST и WS ниже.

## 2. Как запустить на VPS

```bash
# на хосте: часы должны быть синхронизированы до замеров
chronyc tracking
# один раз, до записи (результаты вставить в таблицы ниже)
docker compose run --rm tools geocheck
docker compose run --rm tools netcheck --out /app/data/reports/netcheck.md
docker compose run --rm tools latency  --out /app/data/reports/latency.md
# через неделю записи: сеть по дням из данных рекордера
docker compose run --rm tools report --out /app/data/reports/m1_data.md   # раздел «5. Задержки сети»
```

Повторять `netcheck` и `latency` раз в неделю и после смены провайдера или тарифа VPS.

## 3. Доступность доменов: блокирует ли провайдер или регулятор

`polybot netcheck` проверяет по каждому хосту:
- системный DNS против DNS-over-HTTPS (Cloudflare, Google);
- частные и «bogon»-адреса в ответе;
- TCP-соединение;
- TLS с проверкой сертификата (обрыв рукопожатия → возможна DPI);
- HTTP-статус и `server`;
- код 451 и HTML-страницы не от Cloudflare (возможная страница блокировки);
- для WS — рукопожатие.

Вердикт — подсказка, не доказательство: CDN может отдавать разные IP разным резолверам.

**Результат (заполнить с VPS):**

| цель | хост | IP (системный DNS) | TCP, мс | TLS, мс | HTTP | CF colo | вердикт | заметки |
|---|---|---|---|---|---|---|---|---|
| geoblock | polymarket.com | — | — | — | — | — | — | — |
| clob | clob.polymarket.com | — | — | — | — | — | — | — |
| gamma | gamma-api.polymarket.com | — | — | — | — | — | — | — |
| data-api | data-api.polymarket.com | — | — | — | — | — | — | — |
| docs | docs.polymarket.com | — | — | — | — | — | — | — |
| help | help.polymarket.com | — | — | — | — | — | — | — |
| market-ws | ws-subscriptions-clob.polymarket.com | — | — | — | — | — | — | — |
| sports-ws | sports-api.polymarket.com | — | — | — | — | — | — | — |
| oddspapi | api.oddspapi.io | — | — | — | — | — | — | — |

**Geoblock с VPS** (`polybot geocheck`): вердикт — …, страна — …, регион — … (дата).

Если провайдер блокирует домены, меняем провайдера в разрешённой юрисдикции. **VPN и прокси не используем** (ToS 2.1.4).

## 4. Задержки (заполнить с VPS)

Разовый замер (`polybot latency`, дата, провайдер VPS):

| метрика | n | p50, мс | p95, мс | p99, мс | min | max |
|---|---|---|---|---|---|---|
| TCP connect до edge Cloudflare (clob) | | | | | | |
| REST GET /time, тёплое соединение | | | | | | |
| REST GET /time, новое соединение (TCP+TLS+запрос) | | | | | | |
| REST GET /book, тёплое соединение | | | | | | |
| WS market: PING→PONG | | | | | | |
| WS market: сервер `timestamp` → получение (one-way) | | | | | | |

Неделя записи (раздел «5. Задержки сети» отчёта `polybot report`):

| метрика | n | p50 | p95 | p99 |
|---|---|---|---|---|
| REST GET /time раз в минуту | | | | |
| WS market PING→PONG | | | | |
| WS market one-way | | | | |

**Вывод:** (сравнить с ожиданием 70–100 мс; если p95 REST выше ~150 мс или one-way WS нестабилен — рассмотреть другой дата-центр в той же юрисдикции).
