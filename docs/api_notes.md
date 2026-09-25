# Polymarket: заметки по API (M0)

> Сбор: **2026-09-25** (M0), дополнено на старте M1 (2026-09-25): форматы сверены по коду SDK и реальным ответам Gamma, см. метку [CAP]. Документ живой. Новое допущение об API сначала записывается сюда со ссылкой на источник и только потом попадает в код.
> Правило проекта: в коде не должно быть «знаний по памяти». Константы (тики, комиссии, задержки, лимиты) бот читает из API в рантайме. Если значение приходится задать в конфиге, у него есть ссылка на пункт этого файла.

## Как собраны данные и насколько им верить

Из облачного окружения, где делался M0, закрыт доступ к `docs.polymarket.com`, `help.polymarket.com`, API-хостам `*.polymarket.com` и сайтам поставщиков данных: egress-политика отвечает HTTP 403. Поэтому источники такие:

| Метка | Источник | Доверие |
|---|---|---|
| **[SDK]** | Исходный код официальных SDK с PyPI: `polymarket-client` **0.11.0** (релиз 2026-09-23, репозиторий `github.com/Polymarket/py-sdk`) и `py-clob-client-v2` **1.1.0** (2026-07-17, `github.com/Polymarket/py-clob-client-v2`) | Высокое: именно это SDK отправляет на биржу |
| **[GH]** | Официальный репозиторий Polymarket `github.com/Polymarket/agent-skills` (`order-patterns.md`, `websocket.md`, `ctf-operations.md`) | Высокое, но часть текста написана до V2 (например, упоминается USDC.e) |
| **[DOC?]** | Страницы docs.polymarket.com и help.polymarket.com, прочитанные через поисковую выдачу, а не напрямую | Среднее: **сверить на M1** |
| **[3P]** | Код и документация зрелых сторонних интеграций: NautilusTrader (`docs/integrations/polymarket.md`, ветка develop, прочитано 2026-09-25) | Среднее: практический опыт, но не официальная спецификация |
| **[CAP]** | Реальные ответы Gamma API, записанные третьей стороной: тестовые фикстуры пакета `polymarket-tennis` 0.1.0 (PyPI), сняты 2026-08-18 с `gamma-api.polymarket.com`, урезаны до части полей | Высокое для формата полей, низкое для полноты |
| **[2nd]** | Вторичные источники: новости, блоги, сторонние репозитории | Низкое: только как гипотеза |

Все пункты с метками [DOC?] и [2nd] собраны в чек-лист в §16.

---

## 0. TL;DR: что из этого определяет стратегию

1. **CLOB V2 работает с 2026-04-28.** Старые `py-clob-client` и V1-подписи на проде не работают. Залог теперь **pUSD**, а не USDC.e. Берём официальный SDK `polymarket-client` (§1). [SDK][DOC?]
2. **Спорт: в официальное время старта биржа сама снимает ВСЕ лимитные ордера.** Время старта может сдвигаться, поэтому следить за ним нужно самостоятельно (§12). [GH][DOC?]
3. **Спорт: маркетабельные ордера исполняются с задержкой** (по умолчанию около 3 с; задержку конкретного рынка задаёт поле `secondsDelay`). Пока ордер висит в задержке, его **нельзя отменить** (§12).
   - Для мейкера это окно, чтобы успеть снять котировки после очка или гола.
   - Для нашего режима тейкера это до 3 с риска, который нельзя отменить.

   [GH][DOC?][SDK]
4. **Dead-man switch:** `POST /v1/heartbeats`. Если 10 с (плюс до 5 с буфера) нет валидного heartbeat, биржа снимает все ордера этих API-ключей (§5). [GH][DOC?][SDK]
5. **Комиссии.** Мейкер не платит. Тейкер платит `C · rate · (p(1−p))^exponent`. В спорте с июля 2026: `rate = 0.05` (было 0.03); ребейт мейкерам — 15% собранных тейкер-комиссий (было 25%) (§8). Формула — [SDK], числа — [2nd]. Поэтому параметры читаем с рынка.
6. **Post-only есть** (только для GTC/GTD), поэтому все котировки ставим post-only (§4). [SDK][GH]
7. **Режимы биржи.** Во время рестарта движка — HTTP 425. После рестарта 2 минуты действует режим post-only. В режиме cancel-only POST ордеров отвечает 503 (§6). [SDK][DOC?]
8. **Rate limits.** У каждого signer свой token bucket, тир зависит от 30-дневного объёма. Батч до 15 ордеров принимается только целиком (§7). [SDK][DOC?]
9. **Теннис на polymarket.com.** Walkover или отмена матча — 50-50. Снятие игрока после начала — побеждает проходящий игрок (§12). [2nd, цитата из `description` рынка] Поэтому справедливая цена ≠ no-vig цена букмекера: нужен перевод с учётом правил.
10. **Геоблок:** `GET https://polymarket.com/api/geoblock`. Бот проверяет доступ при старте и периодически, а при блокировке или close-only останавливается (§14). [DOC?]

---

## 1. Версии, миграции, SDK

- **CLOB V2** включён **2026-04-28 около 11:00 UTC**: новые Exchange-контракты, переписанный бэкенд CLOB, новый залог **pUSD**. SDK V1 перестали работать, ордера эпохи V1 удалены при переходе. [DOC?: https://docs.polymarket.com/v2-migration] [2nd: https://www.blockhead.co/2026/04/07/polymarket-overhauls-exchange-stack-with-new-contracts-order-book-collateral-token/]
- Python-SDK:

  | Пакет (PyPI) | Версия / дата | Что это | Решение |
  |---|---|---|---|
  | `py-clob-client` | 0.34.6 / 2026-02-19 | Клиент V1 | **Не использовать** |
  | `py-clob-client-v2` | 1.1.0 / 2026-07-17 | Только CLOB V2 (REST). README сам рекомендует объединённый SDK | Запасной вариант; из него берём `post_heartbeat` |
  | `polymarket-client` (import `polymarket`) | 0.11.0 / 2026-09-23 | **Официальный объединённый SDK**: CLOB, Gamma, Data API, WebSocket (market, user, sports), split/merge/redeem через relayer, session keys. Sync- и Async-клиенты, типизирован (pyright strict), Python ≥ 3.11 | **Основной**. Пиним точную версию: на линии 0.x минорные релизы могут ломать API |

  [SDK: README, pyproject, CHANGELOG]
- Изменения SDK в 2026-09, которые нас касаются [SDK CHANGELOG]:
  - 0.9.0: «Poly V2 identifiers». Появилось новое пространство position id с модулями binary, neg-risk и combinatorial. Ордера на такие активы идут на отдельный контракт `exchange_v3`. У рынка в Gamma есть поле `version` (версия протокола).
  - 0.10.0: чтение из Data API переведено на контракт **Data API v2** (breaking).
  - 0.11.0: `list_markets` корректно сортирует по volume и liquidity.
- **Тик-сайзы** расширены в июле 2026: `{0.1, 0.01, 0.005, 0.0025, 0.001, 0.0001}` [SDK `orders/context.py`, `market_data.py`].

## 2. Хосты (production) [SDK `polymarket/environments.py`]

| Назначение | URL |
|---|---|
| CLOB REST | `https://clob.polymarket.com` |
| CLOB WS, market channel | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| CLOB WS, user channel | `wss://ws-subscriptions-clob.polymarket.com/ws/user` |
| Sports WS (счёт матчей) | `wss://sports-api.polymarket.com/ws` |
| Gamma (рынки и события) | `https://gamma-api.polymarket.com` |
| Data API | `https://data-api.polymarket.com` |
| Relayer (gasless-транзакции) | `https://relayer-v2.polymarket.com` |
| RTDS / realtime (крипто-цены, комментарии; нам не нужно) | `wss://ws-live-data.polymarket.com`, `wss://ws-live-v2.polymarket.com/ws` |
| Polygon RPC по умолчанию в SDK | `https://polygon.drpc.org` |
| Geoblock | `https://polymarket.com/api/geoblock` [DOC?] |
| CLOB, служебные | `GET /ok` (жив ли сервис), `GET /time` (время сервера в секундах: число или объект с `time`/`timestamp`) [SDK V2 `endpoints.py`, `client.py::_get_timestamp`] |

Публичные REST-чтения CLOB, которые использует рекордер [SDK `_internal/actions/clob.py`, `clients/async_public.py`]:
- `GET /book?token_id=…` — книга одного токена;
- `POST /books`, тело `[{"token_id": "…"}, …]` — книги пачкой;
- `GET /clob-markets/{condition_id}` — тик, neg-risk, комиссия (§4);
- `GET /rewards/markets/current` — пагинация через `next_cursor`, элементы в `data`; конец списка — `next_cursor` равен `"LTE="` или отсутствует [SDK `actions/rewards.py`, `actions/_cursor.py`].

## 3. Контракты, Polygon mainnet (chainId 137) [SDK `environments.py`]

Для справки и сверки. Бот адреса не хардкодит: они берутся из SDK.

| Контракт | Адрес |
|---|---|
| Collateral (pUSD) | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
| Conditional Tokens (CTF, ERC-1155) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| CTF Exchange V2 (standard) | `0xE111180000d2663C0091e4f400237545B87B996B` |
| Neg-Risk Exchange V2 | `0xe2222d279d744050d28e00520010520000310F59` |
| Neg-Risk Adapter | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| Collateral Adapter / Neg-Risk Collateral Adapter | `0xAdA100Db00Ca00073811820692005400218FcE1f` / `0xadA2005600Dec949baf300f4C6120000bDB6eAab` |
| Exchange v3 (рынки protocol v2) | `0xe3333700cA9d93003F00f0F71f8515005F6c00Aa` |
| Position Manager (protocol v2) | `0x006F54F7f9A22e0000CC2AB60031000000ae9fEF` |
| Auto-redeem operator | `0xa1200000d0002264C9a1698e001292D00E1b00af` |

Старые адреса V1 (`0x4bFb41d5…` CTF Exchange, `0xC5d563A3…` NegRisk Exchange) остались в `py-clob-client-v2` для совместимости.

## 4. Аутентификация и ордера

**Аутентификация** [SDK `headers.py`, `endpoints.py`; GH `authentication.md`]
- **L1:** EIP-712-подпись кошелька. Нужна для `POST /auth/api-key` и `GET /auth/derive-api-key`. Заголовки: `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`, `POLY_NONCE`.
- **L2:** HMAC-SHA256 по API-кредам (key, secret, passphrase). Заголовки: `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`, `POLY_API_KEY`, `POLY_PASSPHRASE`. Секрет после создания не восстановить.
- Есть **read-only API-ключи** (`/auth/readonly-api-key`). Их используем для рекордера и мониторинга.
- Типы подписи V2: `EOA=0`, `POLY_PROXY=1`, `POLY_GNOSIS_SAFE=2`, `POLY_1271=3` (подпись смарт-контракта). Объединённый SDK по умолчанию работает через **deposit wallet**: смарт-кошелёк плюс gasless relayer.
- **Session keys** (SDK 0.7.0, 2026-08). Для deposit wallet можно авторизовать отдельный ключ со скоупом `CLOB`. Ключ действует 180 дней, для авторизации нужен Builder API key. Это позволяет не держать мастер-ключ на VPS. На M6 проверить, что скоуп `CLOB` не даёт выводить средства.

**Ордера** [SDK `orders/*.py`, `models/clob/orders.py`; GH `order-patterns.md`]
- На бирже все ордера лимитные; «маркет-ордер» — это лимит с маркетабельной ценой. Типы: `GTC`, `GTD`, `FOK`, `FAK`.
- **Post-only:** поле `"postOnly": true` в теле `POST /order`. Работает только с GTC/GTD. Если ордер пересёк бы книгу, биржа его отклоняет (`INVALID_POST_ONLY_ORDER`); с FOK/FAK — ошибка `INVALID_POST_ONLY_ORDER_TYPE`.
- **GTD:** `expiration` в UTC-секундах. Текущий SDK требует expiration ≥ now + **180 с** (релиз 0.1.0b13); в старой доке указан «порог безопасности +60 с».
  - Идея: прематч-котировки ставить GTD с expiration ≤ `gameStartTime`. Это дополнительная страховка к авто-отмене и heartbeat.
- **Батч:** `POST /orders`, не больше **15** ордеров.
- **Структура ордера V2:** `salt, maker, signer, tokenId, makerAmount, takerAmount, side, signatureType, timestamp, metadata, builder, expiration` плюс `signature`. Тело запроса: `{order, owner: <api key>, orderType, deferExec, postOnly?}`. Полей `nonce` и `feeRateBps` в V2 нет.
- **Тики.** Цена должна лежать в `[tick, 1−tick]`, быть кратна тику и иметь не больше знаков после запятой, чем тик. Размер округляется вниз до 0.01 акции.
  - Тик рынка может **только мельчать**, но не укрупняться (инвариант платформы, `py-sdk/AGENTS.md`).
  - Смена тика приходит в WS как `tick_size_change`.
- Минимальный размер ордера — поле рынка `minimumOrderSize`, в книге — `min_order_size`.
- **Параметры рынка одним запросом:** `GET /clob-markets/{condition_id}` возвращает `mts` (тик), `nr` (neg-risk), `t[].t` (token ids), `fd.r` и `fd.e` (ставка и экспонента комиссии).
- **Статусы при вставке:** `live`, `matched`, `delayed` (маркетабельный ордер ждёт задержку), `unmatched` (маркетабельный, задержка не удалась, но ордер размещён).
- **Статусы ордера в user WS:** `LIVE`, `MATCHED`, `DELAYED`, `UNMATCHED`, `CANCELED`. Типы событий: `PLACEMENT`, `UPDATE`, `CANCELLATION`.
- **Статусы сделки:** `MATCHED → MINED → CONFIRMED` (терминальный), ветка `RETRYING`, терминальный `FAILED`. **Позиция окончательна только после `CONFIRMED`**: reconciler учитывает `RETRYING` и `FAILED`.
- **Коды ошибок:** `INVALID_ORDER_MIN_TICK_SIZE`, `INVALID_ORDER_MIN_SIZE`, `INVALID_ORDER_DUPLICATED`, `INVALID_ORDER_NOT_ENOUGH_BALANCE`, `INVALID_ORDER_EXPIRATION`, `INVALID_POST_ONLY_ORDER_TYPE`, `INVALID_POST_ONLY_ORDER`, `FOK_ORDER_NOT_FILLED_ERROR`, `MARKET_NOT_READY`, `ORDER_DELAYED`, `DELAYING_ORDER_ERROR`, `EXECUTION_ERROR`, а также `order_version_mismatch` (константа в SDK V2).
- **Баланс.** Доступный размер = баланс − Σ(открытый размер − исполненный). Для покупки нужен allowance на pUSD, для продажи — на ERC-1155 для соответствующего exchange. В SDK есть `setup_trading_approvals` и `get_trading_approvals_state`.

## 5. Отмена ордеров и heartbeat (dead-man switch)

- **Эндпоинты отмены** (все `DELETE`, нужен L2) [SDK `py_clob_client_v2/client.py`]:
  - `/order` — один ордер;
  - `/orders` — список;
  - `/cancel-all` — все ордера;
  - `/cancel-market-orders` — по `market` (condition_id) и, опционально, `asset_id`.

  → `cancel_all(market)` из промта реализуется через `/cancel-market-orders`.
- **Heartbeat** [GH `order-patterns.md`; DOC?: https://docs.polymarket.com/api-reference/trade/send-heartbeat; SDK V2 `post_heartbeat`]:
  - запрос: `POST /v1/heartbeats`, тело `{"heartbeat_id": "<id>"}`; в первый раз передаётся пустая строка, в ответе приходит id для следующего запроса;
  - если id неверный или просрочен — ответ 400 с правильным id;
  - после первого принятого heartbeat биржа ждёт следующий: если 10 с (плюс до 5 с буфера, проверка идёт раз в 5 с) нет валидного, **снимаются все открытые ордера этих API-ключей**;
  - рекомендуемый интервал — 5 с.

  В `polymarket-client` 0.11.0 метода нет, поэтому реализуем сами поверх L2 HMAC (или через `py-clob-client-v2`).
- Отменить ордер в фазе задержки (`delayed`) нельзя: запрос отклоняется с текстом «can't be canceled because it is pending/delayed». [2nd: https://x.com/PolymarketDevs/status/2061930696323830158 (про крипто-рынки); GH/DOC? — про спорт]
- Резервная отмена on-chain: `cancelOrder(order)` на exchange-контракте. Это медленно и стоит газ, только для аварий. [GH]

## 6. Режимы биржи и рестарты движка

- В SDK есть `TradingRestriction`: `restarting` (движок перезапускается, ордера отклоняются), `cancel_only` (принимаются только отмены), `post_only` (принимаются отмены и post-only). [SDK `errors.py`]
- Во время рестарта — **HTTP 425 Too Early**: ретраить с экспоненциальной паузой. [DOC?: https://docs.polymarket.com/trading/matching-engine]
- После рестарта **2 минуты действует режим post-only**. [DOC?]
- В режиме **cancel-only** `POST /order` и `POST /orders` отвечают **503** («Trading is currently cancel-only…»). Такие ответы требуют сменить логику, а не ретраить вслепую. [DOC?]
- Плановые работы публикуются на `status.polymarket.com`. [2nd]
- Есть `GET /auth/ban-status/closed-only`: аккаунт может попасть в close-only. [SDK]

## 7. Rate limits

- Опубликованные ранее значения: `POST /order` — 3 500 за 10 с (burst) и 36 000 за 10 мин (sustained), окна скользящие. [DOC?: https://docs.polymarket.com/api-reference/rate-limits]
- Схема 2026 года, «CLOB Trading Rate Limits» [DOC?: https://docs.polymarket.com/api-reference/trading-rate-limits; 2nd]:
  - у каждого signer два token bucket: для ордеров и для отмен;
  - скорость пополнения зависит от тира: Standard, Copper, Bronze, Silver, Gold, Platinum, Diamond, Elite;
  - тир определяется 30-дневным объёмом maker-кошелька и пересчитывается раз в 3 часа;
  - батч принимается, только если токенов хватает на все его ордера; иначе отклоняется целиком;
  - батч дороже burst-ёмкости тира не пройдёт никогда.
- Состояние лимитов приходит в заголовках `Poly-RateLimit*`; `Poly-RateLimit-Warning: true` предупреждает до включения жёсткого enforcement. SDK отдаёт это состояние через `on_rate_limit_update`, а при отказе бросает `RateLimitError(retry_after, rate_limit)`. [SDK CHANGELOG 0.7.0]

→ Требование к OrderManager: вести собственный бюджет токенов, ставить отмены выше новых ордеров, дробить батчи.

## 8. Комиссии и мейкер-ребейты

- **Формула** [SDK `py_clob_client_v2/fees.py`, тесты `test_fee_calculations.py`]:
  `fee = C · rate · (p · (1 − p)) ^ exponent`, где C — число акций, p — цена.
  Максимум при p = 0.5, симметрично по p.
- **Где читать параметры рынка:**
  - `GET /clob-markets/{condition_id}` → `fd.r`, `fd.e`;
  - Gamma: `feesEnabled`, `feeType`, `feeSchedule {rate, exponent, takerOnly, rebateRate}`;
  - WS `last_trade_price.fee_rate_bps`.

  [SDK]
- **Спорт (июль 2026):**
  - `rate = 0.05` (было 0.03), то есть максимум $1.25 на 100 акций при p = 0.5;
  - ребейт мейкерам — **15%** тейкер-комиссий (было 25%);
  - для сравнения: крипто 20%, большинство прочих категорий 25%.

  [2nd: https://igamingbusiness.com/prediction-markets/polymarket-sports-fee-hike-2026/ ; https://startpolymarket.com/learn/polymarket-fees/]
- **Как распределяется ребейт:**
  - считается отдельно по каждому рынку;
  - делится пропорционально fee-curve-взвешенному maker-объёму `Σ C·rate·p(1−p)`;
  - выплачивается ежедневно в pUSD, минимум $1.

  [DOC?: https://docs.polymarket.com/programs/maker-rebates]
  → Эффективный ребейт на наше мейкер-исполнение ≈ `0.15 · 0.05 · p(1−p)` на акцию. Это ≈ 0.19 цента на акцию при p = 0.5, то есть ≈ 0.375% нотионала.
- Мейкер комиссию не платит (`takerOnly`). Builder-комиссия берётся только с ордеров с builder-кодом, нам не нужна.

## 9. Награды за ликвидность (liquidity rewards)

- **Параметры рынка:** в Gamma — `rewardsMaxSpread` (v), `rewardsMinSize`, `clobRewards[{rewardsDailyRate, rewardsAmount, startDate, endDate}]`.
- **Эндпоинты CLOB:**
  - `/rewards/markets/current`, `/rewards/markets/{condition_id}` — параметры наград. Элемент `data[]` из `/rewards/markets/current` [SDK `models/clob/rewards.py::CurrentReward`]:
    - общие поля: `condition_id`, `rewards_max_spread`, `rewards_min_size`;
    - `rewards_config[]` — список `{asset_address, start_date, end_date, rate_per_day, total_rewards}`, даты в epoch ms;
    - дневные ставки: `native_daily_rate`, `sponsored_daily_rate`, `total_daily_rate`, плюс `sponsors_count`.

    Единицы `rewards_max_spread` в SDK не указаны; в отчётах выводим как есть;
  - `GET /order-scoring?order_id=…`, `POST /orders-scoring` (тело — список id) — учитывается ли наш ордер для наград;
  - `/rewards/user*` — начисления.

  [SDK]
- **Формула** [DOC?: https://docs.polymarket.com/developers/market-makers/liquidity-rewards; 2nd]:
  - вес ордера: `S(v, s) = ((v − s) / v)² · b`, где s — расстояние до мида, скорректированного по `min size`, а b — in-game множитель;
  - `Q_one = Σ S·BidSize(YES) + Σ S·AskSize(NO)`;
  - `Q_two = Σ S·AskSize(YES) + Σ S·BidSize(NO)`;
  - если мид ∈ [0.10, 0.90]: `Q_min = max(min(Q_one, Q_two), max(Q_one/c, Q_two/c))`, где `c = 3` (одна сторона даёт до 1/3 веса);
  - если мид вне этого диапазона: `Q_min = min(Q_one, Q_two)`, то есть нужны обе стороны;
  - выборка раз в минуту; доля за минуту = `Q_min / ΣQ_min` по всем мейкерам; доли суммируются за эпоху (сутки), награда делится пропорционально; выплата от $1.
- **Спорт:** пул делится на фазы Pre и Live.
  - Пример [2nd]: EPL — $10 000 за матч ($2 800 до матча, $7 200 в лайве); в апреле 2026 на спорт и киберспорт выделено больше $5M.
  - Для **тенниса на polymarket.com** размер пулов неизвестен. Цифра «$1 000 за матч ATP/WTA» относится к **Polymarket US**, это другая площадка. → M1 записывает `/rewards/markets/current`.
- Holding rewards (`holdingRewardsEnabled`, около 4% APR на позиции в отдельных рынках) к спорту, вероятно, не относятся. [SDK field][2nd]

## 10. WebSocket

**Market channel:** `wss://ws-subscriptions-clob.polymarket.com/ws/market` [SDK `streams/clob/market_protocol.py`, `models/clob/market_events.py`; GH `websocket.md`]
- **Подписка.** Первое сообщение: `{"type":"market","assets_ids":[...],"custom_feature_enabled":bool}`. Дальше подписки меняются на лету: `{"operation":"subscribe"|"unsubscribe","assets_ids":[...]}`.
- **События:**
  - `book` — снапшот: `bids`, `asks`, `hash`, `timestamp` (мс), `min_order_size`, `tick_size`, `neg_risk`, `last_trade_price`;
  - `price_change` — поле `price_changes[{asset_id, price, size, side, hash, best_bid, best_ask}]`; `size = "0"` означает, что уровень удалён;
  - `last_trade_price` — `price`, `size`, `side`, `fee_rate_bps`, `transaction_hash`, `timestamp`;
  - `tick_size_change`;
  - только при `custom_feature_enabled`: `best_bid_ask`, `new_market`, `market_resolved` (`winning_asset_id`).
- Один кадр может содержать массив событий.
- Данных по отдельным ордерам (L3) **нет**: только агрегированные уровни L2. Поэтому позиция в очереди в бэктесте и paper-режиме — оценка.
- **Heartbeat:** клиент каждые 10 с шлёт текст `PING`, сервер отвечает `PONG`. Если PONG нет дольше 30 с, соединение считается мёртвым (так делает SDK).

**Market channel: детали, на которые опирается рекордер (M1)**
- **Формат кадра** плоский: `{"event_type": "book", "market": …, "asset_id": …, …}`. Кадр может быть массивом таких объектов. [SDK `market_events._normalize_to_envelope`, `market_protocol.parse_events`]
- **Числа:**
  - цены и размеры приходят строками (`"0.48"`, `".48"`);
  - необязательные десятичные поля бывают пустой строкой `""` — это «нет значения»;
  - `timestamp` — миллисекунды эпохи строкой.

  [SDK `models/clob/_validators.py`, GH]
- **Когда приходит `book`:** при подписке и когда сделка меняет книгу. После реконнекта снапшот приходит заново на каждую подписку. [GH `websocket.md`]
- **`price_change.size`** — новый полный размер уровня, а не приращение. `"0"` удаляет уровень. В каждом изменении есть `best_bid`/`best_ask` после применения — по ним проверяем свою книгу. [GH, SDK `PriceChange`] → сверить на записях (чек-лист §16, п. 14)
- **Сторона:** `BUY` — бид, `SELL` — аск. [SDK]
- **Порядок уровней.** REST `/book`: биды по возрастанию, аски по убыванию, то есть лучшая цена — последняя в списке [SDK `OrderBook` docstring]. Для WS порядок не документирован, поэтому книгу сортируем сами.
- **Номеров последовательности нет.** Пропуски ловим так:
  - разрыв соединения — явная запись о дыре;
  - расхождение нашей книги с `best_bid`/`best_ask` из сообщения;
  - сравнение с REST `/book`: если `hash` из REST равен последнему `hash` из WS для этого токена, уровни тоже должны совпасть.
- **Hash.** Алгоритм (прообраз хэша) не документирован. NautilusTrader воспроизводит хэш только для полных снапшотов; часть обновлений приходит без полей, входящих в прообраз, поэтому их не проверяет [3P]. Мы хэш не пересчитываем, а только сравниваем на равенство.
- **Лимит подписок на соединение** официально не опубликован. У NautilusTrader по умолчанию 200 активов на соединение: при большем числе соединение иногда молча «зависает». Активы шардируются по пулу соединений [3P]. Берём тот же предел: `max_assets_per_conn = 200` в конфиге.
- **Ресинк** — как у NautilusTrader [3P]:
  - после реконнекта или `tick_size_change` старые уровни сбрасываются;
  - дельты игнорируются до свежего `book`;
  - если снапшот не пришёл за 10 с — переподписка на актив.
- **YES и NO.** Книги двух токенов бинарного рынка, по-видимому, зеркальны. Официального подтверждения нет, поэтому рекордер подписывается на оба токена и проверяет зеркальность на данных (чек-лист §16, п. 13).

**User channel:** `.../ws/user`, нужны L2-креды. Подписка по condition_id (`markets`) или на все рынки. События — `order` и `trade`, статусы перечислены в §4; в сделке есть `trader_side`: MAKER или TAKER. [SDK]

**Sports channel:** `wss://sports-api.polymarket.com/ws` [SDK `streams/sports/*`, `models/sports_events.py`; GH; DOC?: https://docs.polymarket.com/market-data/websocket/sports]
- Подписка не нужна: сервер шлёт всем данные по всем играм.
- Heartbeat: сервер шлёт `ping` (по доке раз в 5 с), клиент отвечает `pong` в течение 10 с. SDK считает соединение устаревшим, если ping нет 30 с.
- Поля: `gameId`, `sportradarGameId`, `slug`, `leagueAbbreviation`, `homeTeam`, `awayTeam`, `status`, `live`, `ended`, `score` (строка), `period`, `elapsed`, `finishedTimestamp`, `turn`.
- Кадр — JSON-объект с состоянием одной игры. Heartbeat идёт текстом: сервер шлёт `ping` строчными буквами, клиент отвечает `pong`. [SDK `streams/sports/heartbeat.py`, `models/sports_events.py`]
- **Времени события в кадре нет** (кроме `finishedTimestamp`). Поэтому задержку «очко в реальности → кадр» можно мерить только против внешнего эталона (`docs/data_sources.md`, §4).
- Связь с Gamma — по `gameId`: он есть и в событии Gamma, и в кадре Sports WS.
- **Неизвестно:** гранулярность счёта в теннисе (очки или только геймы) и задержка фида. → Измерить на M1. Даже без очков поток полезен как бесплатный детектор старта и конца матча.

## 11. Gamma API (метаданные рынков)

- **Эндпоинты:**
  - списки: `/events/keyset`, `/markets/keyset` (keyset-пагинация через `next_cursor`);
  - отдельные объекты: `/events/{id}`, `/events/slug/{slug}`, `/markets/{id}`, `/markets/slug/{slug}`;
  - справочники: `/tags/{id}`, `/tags/slug/{slug}`, `/sports`, `/sports/market-types`, `/series/{id}`.

  [SDK `_internal/actions/gamma.py`]
- **Фильтры списков:** `closed`, `tag_id` (+ `related_tags`, `exclude_tag_id`), `game_id`, `sports_market_types`, `start_date_min`, `end_date_min`, сортировка `volume`/`liquidity`/`start_date`/… [SDK]
- **Важные поля рынка** [SDK `models/gamma/market.py`]:

  | Группа | Поля |
  |---|---|
  | Идентификаторы | `id`, `version` (протокол), `conditionId`, `question`, `outcomes`, token ids по исходам, `negRisk` |
  | Правила | **`description`** — текст правил резолюции |
  | Состояние | `active`, `closed`, `acceptingOrders`, `enableOrderBook`, `startDate`, `endDate` |
  | Метрики | `volume*`, `liquidity*`, `bestBid`, `bestAsk`, `lastTradePrice`, `spread` |
  | Торговля | `minimumOrderSize`, `minimumTickSize`, **`secondsDelay`**, `feesEnabled`, `feeType`, `feeSchedule` |
  | Награды | `rewardsMinSize`, `rewardsMaxSpread`, `clobRewards`, `holdingRewardsEnabled` |
  | Спорт | **`sportsMarketType`**, `line`, `gameId`, **`gameStartTime`** |
  | Резолюция | `umaResolutionStatus`, `resolvedBy` |

- Теннис: тег `tag_id=864` [2nd] → проверить через `/tags/slug/tennis`. Типы рынков — moneyline, победитель 1-го сета, тотал геймов, фора по сетам [2nd]. Точные значения `sportsMarketType` → выгрузить `/sports/market-types` на M1.
- В теннисном moneyline исходы — **имена игроков**, а не Yes/No. Порядок исходов нужно сопоставлять явно (в SDK был фикс «preserve team ordering»).

**Gamma: детали, на которые опирается рекордер (M1)**
- **Keyset-пагинация** [SDK `actions/gamma.py`, `_internal/request.py`, `_internal/dispatch.py`]:
  - запрос: `GET /events/keyset` (или `/markets/keyset`) с параметрами `limit` и `after_cursor` (значение `next_cursor` из прошлого ответа);
  - ответ: `{"events": [...], "next_cursor": "…"}` (для рынков — ключ `markets`); если `next_cursor` нет или он `null`, страниц больше нет;
  - фильтры событий: `tag_id` (можно несколько), `closed`, `live`, `ended`, `start_time_min`/`start_time_max`, `start_date_min`/`start_date_max`, `order`, `ascending` и др.
- **Имена полей в сыром JSON** иногда отличаются от атрибутов SDK. Рекордер читает сырой JSON [SDK `models/gamma/market.py::_normalize_market`]:
  - `outcomes`, `outcomePrices`, `clobTokenIds` (и `positionIds`) — **JSON-массивы, закодированные строкой**: `"[\"Jiri Lehecka\", \"Arthur Fils\"]"` [SDK `parse_sequence`, CAP];
  - минимальный размер ордера — `orderMinSize`, тик — `orderPriceMinTickSize` (в M0 выше указаны имена атрибутов SDK: `minimumOrderSize`/`minimumTickSize`);
  - `questionID`, `negRiskRequestID`, `umaResolutionStatus`, `resolvedBy`, `secondsDelay`, `feesEnabled`, `feeType`, `feeSchedule`, `clobRewards`, `rewardsMinSize`, `rewardsMaxSpread`, `sportsMarketType`, `line`, `gameId`, `gameStartTime`.
- **Формат `gameStartTime`** в реальном ответе: `"2026-08-18 01:15:00+00"` — пробел вместо `T`, смещение `+00` [CAP]. Прочие даты — ISO 8601 с `Z` (`"2026-08-24T14:00:00Z"`) [CAP]. Парсер должен принимать оба формата; непарсящееся время → рынок не котируется.
- **Спортивные поля события** [SDK `models/gamma/event.py`]:
  - идентификаторы и расписание: `gameId`, `sportsradarMatchId`, `rescheduledFromGameId`, `startTime`, `eventDate`;
  - состояние игры: `gameStatus`, `live`, `ended`, `score`, `period`, `elapsed`, `finishedTimestamp`;
  - участники: `homeTeamName`, `awayTeamName`, `teams[]`.

  `startDate` события — время создания рынка, а не старт матча: `"2026-08-16T05:05:18Z"` при `gameStartTime` 2026-08-18 01:15 [CAP].
- **`sportsradarMatchId`** — ID матча в Sportradar/Betradar. Если такой же ID есть у поставщика коэффициентов (у OddsPapi: `externalProviders.betradarId`, см. `docs/data_sources.md`, §5), сопоставление становится точным. → проверить заполненность по видам спорта и турам на M1.
- **Теннис, наблюдения по реальным ответам** [CAP]:
  - у матчевых событий теги `tennis` (id 864), `sports` (1), `games` (100639); у фьючерсов вместо `games` — `atp` (101232);
  - slug: `atp-lehecka-fils-2026-08-17`, `wta-eala-anisimo-2026-08-17`, `atp-doubles-cashgla-ramsali-2026-08-16`; дата в slug — местная дата матча, она может не совпадать с датой `gameStartTime` в UTC;
  - **матчи Challenger/ITF тоже идут с префиксом `atp-`** (например, «Roehampton: …», «Sion: …»), поэтому уровень турнира по slug не определить — он берётся из сопоставления с поставщиком коэффициентов;
  - парный разряд: `(Doubles)` в заголовке и `atp-doubles-` в slug;
  - значения `sportsMarketType`: `moneyline`, `tennis_completed_match`, `tennis_first_set_winner`, `tennis_set_winner`, `tennis_set_totals`, `tennis_first_set_totals`, `tennis_set_games_totals`, `tennis_match_totals`, `tennis_set_handicap`; у фьючерсов `sportsMarketType` и `gameStartTime` пустые;
  - **рынок «Completed Match» (Yes/No)** — вероятность, что матч будет доигран. Пригодится для приоров `p_wo` и `r` в `rules_adjust` (`docs/architecture.md`, §4.1).
- **Справочники:**
  - `/sports` возвращает `{id, sport, image, resolution, ordering, tags, series}`; `tags` и `series` — строки, формат списка не задокументирован [SDK `SportsMetadata`];
  - `/sports/market-types` возвращает `{"marketTypes": [...]}` [SDK].
- По опыту стороннего автора, фильтр `tag_slug` в URL ненадёжен и лучше передавать числовой `tag_id` [2nd: polymarket-tennis]. Поэтому рекордер при старте получает id по `/tags/slug/{slug}` и не запускается, если slug не найден.

## 12. Спортивные рынки: поведение и правила

- **Авто-отмена:** в официальное время старта биржа снимает все лимитные ордера, книга очищается. **Время старта может сдвигаться**, за ордерами нужно следить самостоятельно. [GH `order-patterns.md` «Sports Markets»; DOC?: https://help.polymarket.com/en/articles/13364444-limit-orders]
  - Для тенниса это важно: матчи «после предыдущего» (followed by) нередко начинаются **раньше** расчётного времени.
- **Задержка маркетабельных ордеров** в спорте — около **3 с**, у конкретного рынка задаётся полем `secondsDelay`. Ордер в задержке отменить нельзя. Упоминается тест 1-секундной задержки на NBA/MLB. [GH; DOC?: https://docs.polymarket.com/concepts/order-lifecycle; SDK: `secondsDelay`, `DELAYED`]
- Отдельная тейкер-задержка 250 мс, снижена до 50 мс с 2026-08-17, действует на **крипто и финансовых** рынках, не в спорте. [2nd]
- **Теннис, polymarket.com.** Правила — дословно из `description` ATP-рынка в Gamma (получено сторонним автором 2026-08-23) [2nd: https://github.com/livetennisapi/polymarket-tennis/blob/main/skills/polymarket-tennis/references/settlement-rules.md]:
  > If the match is canceled (not played at all), ends in a tie, or is delayed beyond 7 days from the scheduled date without a winner determined, this market will resolve to 50-50.
  > If the match begins but is not completed, and one player advances due to the opponent's retirement, default, or disqualification, this market will resolve to the player who advances.
  > If the match ends in a walkover (player withdraws before the start and the other advances automatically), this market will resolve to 50-50.
  > The primary resolution source will be official information from the ATP Tour.

  - На **Polymarket US** правила другие: walkover там разрешается по последней справедливой цене. Не путать площадки.
  - У букмекеров правила по снятию (retirement) бывают разные, поэтому перевод no-vig вероятностей в цену Polymarket делается с поправками (см. `docs/architecture.md`, §4).
- **Футбол:** три бинарных рынка — победа A, ничья, победа B; обычно это neg-risk событие. Итог считается по основному времени с добавленным. [2nd] → сверить по `description`.
- **Принцип:** правила берутся из `description` каждого рынка. `rules_parser` классифицирует текст по известным шаблонам. Незнакомый текст — рынок не котируется, пока его не проверит человек.

## 13. CTF: split / merge / redeem в V2

- **pUSD** — ERC-20 на Polygon, обеспечен USDC 1:1. API-трейдер получает его, обернув USDC.e через Collateral Onramp (`wrap()`). [DOC?: https://docs.polymarket.com/v2-migration; 2nd]
- В V2 split, merge и redeem идут через **collateral adapters** (у neg-risk свой адаптер), а для рынков protocol v2 — через `position_manager`.
  - В SDK: `split_position`, `merge_positions`, `merge_multiple_positions` (батч), `redeem_positions`. Для deposit wallet они работают через gasless relayer.
  - **Вызовы контрактов руками не пишем.** [SDK `_internal/actions/relayer/positions.py`, `environments.py`]
- `redeem` сжигает весь баланс позиции по условию; срока нет. [GH `ctf-operations.md`]
- В конфиге есть `auto_redeem_operator`. Проверить на M1, редимит ли платформа выигрыши автоматически. [SDK]

## 14. Комплаенс: геоблок

- `GET https://polymarket.com/api/geoblock` возвращает статус для IP: страну, регион и признак блокировки. Эндпоинт на `polymarket.com`, а не на API-хостах. [DOC?: https://docs.polymarket.com/api-reference/geoblock]
- **Формат ответа:** `{"blocked": bool, "ip": str, "country": str, "region": str}`, например `{"blocked": true, "ip": "203.0.113.42", "country": "US", "region": "NY"}`. [DOC? через поисковую выдачу]
  - Отдельного признака close-only в этих четырёх полях нет. Поэтому гейт проекта (§ ниже) дополнительно сверяет `country` со списком стран, разрешённых оператором.
- По вторичным обзорам, Латвия на 2026-09 доступна [2nd]; про Армению данных нет. Решает только ответ geoblock с VPS и официальный список на help.polymarket.com (https://help.polymarket.com/en/articles/13364163-geographic-restrictions).
- Ограничения бывают двух видов: полный запрет (нельзя даже закрывать позиции) и close-only (только закрытие). Список стран меняется; в нём санкционные юрисдикции и ряд стран ЕС. [2nd]
- **Решение проекта (подтверждено пользователем 2026-09-25):**
  - бот проверяет geoblock при старте и раз в N минут (`config/base.yaml` → `geoblock`);
  - **старт разрешён**, только если ответ получен и разобран, `blocked == false` и `country` входит в `allowed_countries`. Для VPS в Ереване это `["AM"]`, список задаёт оператор;
  - любое другое состояние (ошибка сети, непонятный ответ, `blocked == true`, чужая страна) — процесс не стартует. Если такое состояние возникло во время работы: запрет новых ордеров, `cancel_all`, алерт; рекордер останавливается;
  - никаких обходов через VPN или прокси: это нарушение ToS (раздел 2.1.4) и риск заморозки средств;
  - оператор и VPS должны находиться в юрисдикции, где торговля разрешена. Geoblock проверяет только IP сервера, поэтому оператор отдельно проверяет свою юрисдикцию (Латвия или Армения) по официальному списку.

## 15. Инфраструктура и задержки

- По сторонним измерениям, CLOB стоит в **AWS eu-west-2 (Лондон)** за Cloudflare [2nd: https://x.com/fulldecent/status/2043928173620945299]. Регион VPS выбирать по двум условиям: разрешённая юрисдикция (§14) и минимальный замеренный RTT до CLOB. Замер — задача M1.
- На M1 измерить:
  - RTT `POST /order` и `DELETE /cancel-market-orders`;
  - время от отмены до события `CANCELLATION` в user WS;
  - задержку WS-событий относительно `timestamp` в сообщениях.
- **Cloudflare перед API.** TCP- и TLS-рукопожатие измеряют RTT только до ближайшей точки присутствия Cloudflare, а не до origin в Лондоне. До origin доходят только некэшируемые запросы (`GET /time`, `GET /book`), поэтому для оценки «до биржи» берём их TTFB.
  - Заголовок `cf-ray` заканчивается кодом дата-центра Cloudflare (например, `…-FRA`) [Cloudflare docs: https://developers.cloudflare.com/fundamentals/reference/http-headers/#cf-ray]. Логируем его, чтобы видеть маршрут.
  - SDK распознаёт блокировку Cloudflare по заголовку `server: cloudflare` и HTML-телу ответа [SDK `clients/_transport.py`].
- Методика и результаты замеров с VPS — `docs/latency.md`.

## 16. Чек-лист «Сверить на M1» (открыть страницы и API напрямую с VPS)

| # | Утверждение | Метка | Как проверить |
|---|---|---|---|
| 1 | Авто-отмена лимиток на старте и её точный момент: `gameStartTime` или фактический старт | DOC?/GH | Записать книгу и user-события на 20+ матчах вокруг `gameStartTime`, сравнить со Sports WS |
| 2 | Задержка маркетабельных ордеров в теннисе и футболе (значение `secondsDelay`); что происходит с отменой в фазе delay | DOC? | Выгрузить `secondsDelay` по спортивным рынкам; на Live-S — единичные тестовые ордера |
| 3 | Параметры комиссии спорта: `fd.r`, `fd.e`, rebateRate | 2nd | `GET /clob-markets/{cid}` и `feeSchedule` по 100+ рынкам |
| 4 | Формула наград, c=3, b (in-game), пулы по теннису | DOC?/2nd | Страница liquidity-rewards; `/rewards/markets/current` |
| 5 | Heartbeat: 10 с + буфер, область действия (API-ключ) | DOC?/GH | Страница send-heartbeat; тест на paper-ключе с малым ордером (Live-S) |
| 6 | Тиры rate limits и заголовки `Poly-RateLimit*` | DOC? | Страница trading-rate-limits; логировать заголовки |
| 7 | Рестарты: 425, 2 мин post-only, 503 cancel-only | DOC? | Страница matching-engine; журнал status.polymarket.com |
| 8 | Правила тенниса и футбола | 2nd | Сохранять и классифицировать `description` всех записанных рынков (M2) |
| 9 | Гранулярность и задержка Sports WS в теннисе | — | Писать Sports WS параллельно с платным фидом (M1/M7) |
| 10 | Тег тенниса и значения `sportsMarketType` | 2nd | `/tags/slug/tennis`, `/sports/market-types` |
| 11 | Геоблок с VPS | DOC? | `GET /api/geoblock` с сервера перед любым ключом |
| 12 | pUSD: wrap и unwrap, auto-redeem | DOC?/SDK | Страница v2-migration; SDK `plan_collateral_return` |
| 13 | Книги YES и NO одного рынка зеркальны | — | Рекордер пишет оба токена; `report` сравнивает bid(YES) и 1 − ask(NO) |
| 14 | `price_change.size` — полный размер уровня, а не приращение | GH/SDK | Проверка книги по `best_bid`/`best_ask` из сообщений и по REST `/book` при равных `hash` |
| 15 | Значения `sportsMarketType` и теги для футбола и баскетбола | — | `polybot discover` на VPS; значения — в `config/recorder.yaml` |
| 16 | Предел активов на одно WS-соединение (у нас 200) | 3P | Считать «тихие» соединения (нет кадров при живом PONG) и сравнить с числом подписок |
| 17 | Формат `gameStartTime` и других дат во всех видах спорта | CAP | Парсер логирует каждое непарсящееся значение; в отчёте — их число |
| 18 | Заполненность `sportsradarMatchId` (Gamma) и совпадение с `betradarId` (OddsPapi) | SDK/2nd | Отчёт сопоставления M1 |

## Источники

- SDK: https://pypi.org/project/polymarket-client/ (0.11.0), https://github.com/Polymarket/py-sdk ; https://pypi.org/project/py-clob-client-v2/ (1.1.0), https://github.com/Polymarket/py-clob-client-v2
- GH: https://github.com/Polymarket/agent-skills (`order-patterns.md`, `websocket.md`, `ctf-operations.md`, `authentication.md`)
- DOC? (через поиск): https://docs.polymarket.com/v2-migration · https://docs.polymarket.com/concepts/order-lifecycle · https://docs.polymarket.com/trading/orders/overview · https://docs.polymarket.com/api-reference/trade/send-heartbeat · https://docs.polymarket.com/api-reference/rate-limits · https://docs.polymarket.com/api-reference/trading-rate-limits · https://docs.polymarket.com/trading/matching-engine · https://docs.polymarket.com/programs/maker-rebates · https://docs.polymarket.com/developers/market-makers/liquidity-rewards · https://docs.polymarket.com/market-data/websocket/sports · https://docs.polymarket.com/api-reference/geoblock · https://docs.polymarket.com/changelog · https://help.polymarket.com/en/articles/13364444-limit-orders · https://help.polymarket.com/en/articles/13364478-trading-fees
- 2nd: https://igamingbusiness.com/prediction-markets/polymarket-sports-fee-hike-2026/ · https://startpolymarket.com/learn/polymarket-fees/ · https://x.com/PolymarketDevs/status/2061930696323830158 · https://x.com/PolymarketDevs/status/2089295325660172578 · https://github.com/livetennisapi/polymarket-tennis · https://x.com/fulldecent/status/2043928173620945299 · https://www.cointech2u.com/polymarket-to-distribute-over-5-million-in-liquidity-rewards-for-sports-and-esports-markets-in-april/
