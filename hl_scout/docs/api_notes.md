# Hyperliquid: заметки по API для hl_scout

> Сбор: **2026-09-25**. Сверка с официальной документацией: **2026-09-26** — страницы прочитаны напрямую (к адресу страницы добавляется `.md`). Правило проекта: в коде нет «знаний по памяти». Каждый факт об API сначала записан здесь со ссылкой и меткой доверия, потом используется в коде.

## Как собраны данные и насколько им верить

25 сентября из облачного окружения были закрыты `hyperliquid.gitbook.io`, `api.hyperliquid.xyz` и `stats-data.hyperliquid.xyz`, и факты собирались по SDK и выдержкам. 26 сентября доступ открыт: всё, что было помечено [DOC-S], сверено с самими страницами документации и переведено в [DOC], а поведение живого API проверено командой `selfcheck` и реальным запуском (метка [LIVE]). `api.telegram.org` по-прежнему закрыт.

| Метка | Источник | Доверие |
|---|---|---|
| **[DOC]** | Официальная документация, страница прочитана целиком 2026-09-26. Страницы: `for-developers/api/info-endpoint`, `…/info-endpoint/perpetuals`, `…/rate-limits-and-user-limits`, `…/websocket`, `…/websocket/subscriptions`, `…/websocket/timeouts-and-heartbeats`, `…/nonces-and-api-wallets`, `…/tick-and-lot-size`, `…/error-responses`, `trading/fees`, `trading/funding`, `trading/margining`, `trading/liquidations`, `trading/builder-codes` (все под `https://hyperliquid.gitbook.io/hyperliquid-docs/`) | Высокое |
| **[LIVE]** | Наблюдение на живом API 2026-09-26 (`selfcheck`, запуск discovery, ручные запросы) | Высокое для факта, но может измениться |
| **[3P-UI]** | Интерфейс стороннего сервиса (JS-бандл его веб-приложения, мета-описание сайта) | Среднее: так показывает сам сервис, но это не документация |

Источники первого сбора:

| Метка | Источник | Доверие |
|---|---|---|
| **[SDK]** | Официальный Python SDK `hyperliquid-dex/hyperliquid-python-sdk`, коммит `2fdb18f` (2026-06-04): `hyperliquid/info.py`, `api.py`, `websocket_manager.py`, `utils/types.py`, `utils/constants.py` | Высокое: так ходит сам SDK биржи |
| **[SDK-TS]** | TypeScript SDK `@nktkas/hyperliquid` 0.33.3 (npm). Типы ответов с комментариями и ссылками на разделы официальной документации | Среднее-высокое: сообщество, но схемы подробные и со ссылками на docs |
| **[DOC-S]** | Официальная документация по выдержкам поисковика (страница была закрыта). Все такие пункты 2026-09-26 сверены и переведены в [DOC] | — |
| **[3P]** | Сторонний код: `cordilleradev/hyperliquid-go`; три торговых бота из §10 | Низкое-среднее |
| **[2nd]** | Статьи и обзоры | Низкое: только гипотеза, в коде как параметр конфига |

## 1. Хосты

| Назначение | URL | Источник |
|---|---|---|
| Info API (REST, `POST`, JSON) | `https://api.hyperliquid.xyz/info` | [SDK `constants.py`, `api.py`] |
| WebSocket | `wss://api.hyperliquid.xyz/ws` | [SDK `websocket_manager.py`: `"ws" + base_url[len("http"):] + "/ws"`] |
| Лидерборд (неофициальный, см. §6) | `https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` (`GET`) | [3P][2nd] |
| Страница кошелька (официальный эксплорер) | `https://app.hyperliquid.xyz/explorer/address/<адрес>` | [2nd: выдача поисковика со страницами этого вида] |

## 2. Info-запросы, которые использует hl_scout

Все запросы — `POST /info` с телом `{"type": ..., ...}` [SDK `info.py`].

| type | Тело запроса | Что берём из ответа | Источник |
|---|---|---|---|
| `clearinghouseState` | `user`, `dex` (`""` = основной perp DEX) | `marginSummary.accountValue`, `totalNtlPos`; `crossMaintenanceMarginUsed`; `assetPositions[].position`: `coin`, `szi` (со знаком), `entryPx`, `positionValue`, `unrealizedPnl`, `liquidationPx` (может быть `null`), `leverage{type: cross/isolated, value}`, `marginUsed`, `maxLeverage`, `cumFunding{allTime, sinceOpen, sinceChange}`; `time` | [SDK docstring][SDK-TS] |
| `spotClearinghouseState` | `user` | `balances[]`: `coin`, `token`, `total`, `hold`, `entryNtl` | [SDK][SDK-TS] |
| `userFillsByTime` | `user`, `startTime`, `endTime` (мс), `aggregateByTime` (bool); есть и `reversed` | Филлы: `coin`, `px`, `sz`, `side` (`B`/`A`), `time`, `startPosition` (позиция ДО филла, со знаком), `dir`, `closedPnl`, `hash`, `oid`, `crossed` (true = тейкер), `fee` (отрицательный = ребейт), `feeToken`, `tid`, `builderFee?`, `twapId`, `liquidation?{liquidatedUser?, markPx, method: market/backstop}`, `cloid?` | [SDK `types.py` Fill][SDK-TS `UserFill`] |
| `portfolio` | `user` | Массив пар `[период, {accountValueHistory: [[мс, "число"]], pnlHistory: [[мс, "число"]], vlm}]`, периоды: `day`, `week`, `month`, `allTime`, `perpDay`, `perpWeek`, `perpMonth`, `perpAllTime`. Точек ~40–130 на окно: шаг от ~20 мин (`day`) до нескольких часов и недель (`allTime`) [LIVE] | [SDK метод][SDK-TS тип][DOC] |
| `userNonFundingLedgerUpdates` | `user`, `startTime`, `endTime` | `{time, hash, delta}`; `delta.type`: `deposit`, `withdraw`, `internalTransfer` (`user`, `destination`), `subAccountTransfer` (`user`, `destination`), `spotTransfer`, `send` (`user`, `destination`, `usdcValue`), `accountClassTransfer`, `liquidation` (`liquidatedNtlPos`, `accountValue`, `leverageType`, `liquidatedPositions`), `vaultCreate`, `vaultDeposit`, `vaultWithdraw`, `vaultDistribution`, `rewardsClaim` и др. | [SDK][SDK-TS] |
| `userFunding` | `user`, `startTime`, `endTime` | `{time, hash, delta{type: "funding", coin, usdc, szi, fundingRate, nSamples}}` | [SDK][SDK-TS] |
| `fundingHistory` | `coin`, `startTime`, `endTime` | `{coin, fundingRate, premium, time}` | [SDK][SDK-TS] |
| `candleSnapshot` | `{"req": {coin, interval, startTime, endTime}}` | `t` (открытие), `T` (закрытие), `s`, `i`, `o`, `h`, `l`, `c` (строки), `v`, `n` | [SDK][SDK-TS] |
| `metaAndAssetCtxs` | — | `[meta, ctxs]`: `meta.universe[]` = `name`, `szDecimals`, `maxLeverage`, `marginTableId`, `onlyIsolated?`, `isDelisted?`, `marginMode?`; `meta.marginTables`; `ctxs[]` = `markPx`, `midPx`, `oraclePx`, `funding`, `dayNtlVlm`, `openInterest`, `prevDayPx` (порядок совпадает с `universe`) | [SDK docstring][SDK-TS] |
| `spotMetaAndAssetCtxs` | — | Спот-пары, токены и цены — для оценки спот-балансов в детекторе хеджей | [SDK] |
| `userRole` | `user` | `role`: `missing` / `user` / `agent` / `vault` / `subAccount`. У `agent` есть `data.user` — адрес мастер-аккаунта, у `subAccount` — `data.master`. Частая ошибка — запрашивать данные по адресу API-кошелька (агента): ответ будет пустым, нужен адрес мастер-аккаунта (раздел «User address») | [SDK метод][SDK-TS][DOC] |
| `allMids` | `dex` (`""`) | `{монета: "mid"}` для всех торгуемых монет; weight 2 — самый дешёвый способ получить текущие цены | [SDK][DOC] |
| `subAccounts` | `user` | `[{name, subAccountUser, master, clearinghouseState{marginSummary{accountValue, totalNtlPos, …}, assetPositions}, spotState}]` — субаккаунты мастер-аккаунта. Нужен, потому что строка лидерборда мастера суммирует субаккаунты (§6) | [DOC «Retrieve a user's subaccounts»][LIVE] |
| `userFills` | `user`, `aggregateByTime` | **Не больше 2000 последних филлов**, от новых к старым [LIVE]. Используется в проверке маркет-мейкеров (`mm`): нужна свежая выборка без знания времени | [DOC «Retrieve a user's fills»][LIVE] |
| `extraAgents` | `user` | `[{name, address, validUntil}]` — API-кошельки (агенты), одобренные аккаунтом; `validUntil` — мс или `null` (бессрочно). В официальной документации не описан | [SDK `info.py` extra_agents][SDK-TS] |

### Имена монет в филлах
- Основные перпы — `BTC`, `ETH`, ...
- Спот — `@<index>` или `PURR/USDC` [SDK `info.py`: спот-активы и имена пар].
- Перпы builder-deployed DEX (HIP-3) — `<dex>:<COIN>`, например `xyz:AAPL` [SDK-TS `perpDexs`; SDK пример `test:ABC`].
- hl_scout копирует только основные перпы. Спот-филлы идут только в детектор хеджей. Филлы HIP-3 считаются неповторимыми (настройка `universe.allow_hip3`).

## 3. Лимиты выдачи и пагинация

- `userFillsByTime`: «Returns at most 2000 fills per response and only the 10000 most recent fills are available» [DOC]. Следствие: у очень активных кошельков 90 дней истории может не быть. Такой кошелёк помечается `history_truncated` и не проходит фильтр «90 дней» (fail-closed). `startTime` и `endTime` включительные [DOC].
- `userFills`: «Returns at most 2000 most recent fills» [DOC].
- Запросы с диапазоном времени (`fundingHistory`, `userFunding`, `userNonFundingLedgerUpdates`): «Responses that take a time range will only return 500 elements or distinct blocks of data. To query larger ranges, use the last returned timestamp as the next `startTime` for pagination» [DOC «Pagination»].
- `candleSnapshot`: «Only the most recent 5000 candles are available» [DOC]. Проверено: 1m-свечей ровно 5001 за ~3,5 дня [LIVE]. Интервалы: `1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d 3d 1w 1M` [SDK-TS][DOC]. Следствие для бэктеста:

  | Интервал | Глубина |
  |---|---|
  | 1m | ≈ 3,5 дня |
  | 15m | ≈ 52 дня |
  | 1h | ≈ 208 дней |

  Цена внутри длинной свечи интерполируется, поэтому точность задержки 10–30 с на старой истории ниже (см. `docs/methodology.md`).

## 4. Rate limits

| Правило | Значение | Источник |
|---|---|---|
| Общий бюджет REST | **1200 weight в минуту на IP** («REST requests share an aggregated weight limit of 1200 per minute») | [DOC] |
| weight 2 | `l2Book`, `allMids`, `clearinghouseState`, `orderStatus`, `spotClearinghouseState`, `exchangeStatus` | [DOC] |
| weight 60 | `userRole` | [DOC] |
| weight 20 | все остальные документированные info-запросы | [DOC] |
| Доплата за объём | +1 за каждые 20 элементов ответа: `recentTrades`, `historicalOrders`, `userFills`, `userFillsByTime`, `fundingHistory`, `userFunding`, `nonUserFundingUpdates`, `twapHistory` и др.; `candleSnapshot` — +1 за каждые 60 свечей | [DOC] |
| WebSocket | не больше 10 соединений; не больше 30 новых соединений в минуту; не больше 1000 подписок; **не больше 10 уникальных пользователей** в пользовательских подписках; не больше 2000 исходящих сообщений в минуту | [DOC] |

Как считается «минута», документация не говорит. В коде — журнал за последние 60 с (`hl/ratelimit.py`): ни в одном 60-секундном окне не больше `api.weight_budget_per_min` (1100 из 1200). Максимальная доплата за объём резервируется до запроса и заменяется фактической после ответа. Прежний token bucket в первую минуту пропускал двойной бюджет — на реальном запуске это дало HTTP 429 [LIVE]. Для лидерборда (другой хост) лимит неизвестен, поэтому запрашиваем редко и кэшируем.

## 5. WebSocket

- Подписка: `{"method": "subscribe", "subscription": {...}}`, отписка — `"unsubscribe"` [SDK].
- Подтверждение подписки: кадр `{"channel": "subscriptionResponse", "data": {"method": "subscribe", "subscription": <эхо>}}` [DOC][LIVE]. Ошибки приходят кадром `{"channel": "error", "data": "<текст>"}`. После переподключения сервер может отклонить повторную подписку, поэтому подтверждения надо проверять заново [SDK-TS `transport/websocket`].
- Сервер может разорвать соединение без предупреждения; клиент обязан переподключаться. Пропущенное за время обрыва приходит в снимке при повторной подписке, его же можно дочитать info-запросом [DOC «Websocket»].
- Ping: `{"method": "ping"}`, ответ — канал `pong`.
  - **Сервер закрывает соединение, если сам ничего не отправлял в него 60 с**; для тихих каналов нужен ping [DOC «Timeouts and heartbeats»].
  - Официальный SDK шлёт ping каждые 50 с [SDK]. TS SDK — каждые 30 с и переподключается, если pong не пришёл за 10 с («полуоткрытое» соединение) [SDK-TS].
  - hl_scout: ping каждые 30 с, ожидание pong 10 с, при тишине — переподключение с повторной подпиской.
- `{"type": "trades", "coin": "BTC"}` → канал `trades`, `data` = список сделок `{coin, side, px, sz, time, hash, tid, users: [покупатель, продавец]}` [DOC: `users: [string, string] // [buyer, seller]`][LIVE]. Этим каналом discovery собирает адреса из крупных сделок.
- `{"type": "userFills", "user": ...}` → канал `userFills`, `data = {user, isSnapshot, fills[]}` [SDK `types.py`].
  - Первый кадр после (пере)подписки — снимок с `isSnapshot: true`, дальше `isSnapshot: false` [DOC]. Это не новые сделки, а история.
  - Филлы, пропущенные за время обрыва, WS не присылает. monitor после переподключения должен дочитать их через REST `userFillsByTime` от последнего увиденного времени.

## 6. Лидерборд (неофициальный эндпоинт)

- `GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` → `{"leaderboardRows": [{ethAddress, accountValue (строка), windowPerformances: [[окно, {pnl, roi, vlm}], ...], prize, displayName}]}`. Окна: `day`, `week`, `month`, `allTime` [3P: `hyperliquid-go`, `types/leaderboard.go` и `rest/api.go`][2nd].
- Эндпоинт **не описан** в документации публичного API: им пользуется веб-интерфейс лидерборда. Поэтому:
  - окна разбираем по имени, а не по индексу;
  - на отсутствующие поля код не падает;
  - если эндпоинт недоступен, discovery работает дальше на адресах из крупных сделок и ручном списке.
- Правила попадания в лидерборд неизвестны. Остаточный survivorship bias — см. `docs/methodology.md` §6. Наблюдения 2026-09-26 [LIVE]:
  - 46 867 строк. В лидерборде есть и проигравшие: у ~22 тыс. строк PnL за всё время отрицательный, у ~22,6 тыс. капитал меньше $1k.
  - **Строка мастер-аккаунта суммирует его субаккаунты** (капитал, PnL, оборот), а сами субаккаунты отдельными строками не показаны. Пример: `0x85ec…2052` — в строке капитал $60,3M и оборот $40,9B за месяц, а `portfolio` самого адреса показывает капитал ~$0,04M, его последние собственные филлы — апрель 2026. `subAccounts` возвращает 50 субаккаунтов, крупнейшие с капиталом $7–9M. Ни один из проверенных субаккаунтов в лидерборде не найден.
  - Следствие для discovery: если собственный оборот адреса (`portfolio`, окно `month`) намного меньше оборота его строки, торгуют субаккаунты. Их берём через `subAccounts` и проверяем по отдельности (`discovery.subaccounts` в `config.yaml`). Копировать можно только конкретный торгующий адрес.

## 6b. Copy-бот ApexLiquid (сторонний сервис)

Поля раздела 13 ТЗ совпадают с формой «Create a Copy Trade» бота ApexLiquid (`https://apexliquid.bot`, Telegram `@Apexliquid_bot`). Источник — JS-бандл его веб-приложения, прочитан 2026-09-26 [3P-UI]:

- **Размер копии**: подсказка под полем Copy Ratio — «Your Copy Size = (Target Size ÷ Target Balance) × Your Balance × Copy Ratio». Copy Ratio от 0,01 до 10. Семантика зафиксирована в `copybot_fields.yaml` (`ratio_applies_to: balance_scaled`), симулятор считает по ней.
- Модель настроек бота: `target_address`, `tag` (до 20 символов), `reverse_copy`, `copy_ratio`, `copy_existing_position`, `tp_trigger`/`sl_trigger` (%, 0–100), `tp_balance`/`sl_balance` ($), `limit_buy_lower_price`/`limit_sell_higher_price` (%), `limit_buy_change_market`/`limit_sell_change_market` (с), `min_trade_size`/`max_trade_size` ($), `buy_times_per_token`, `min_size_copy_buy` («Your Copy Size < $10: Buy»), `max_size_per_token`/`max_margin_per_token` ($), `follow_leverage`, `max_total_margin` ($), `copy_long`/`copy_short`, `target_min_balance` ($), `limit_buy_lower_price_only_open`.
- Поведение полей интерфейс не описывает. Документация бота — `https://apexliquid.gitbook.io/apexliquid`, из облачного окружения закрыта (egress 403).
- **Топ трейдеров бота** (неофициально, эндпоинт его веб-приложения): `POST https://apexliquid.bot/v1/web/top_trades` с телом `{}` → `{"code": 0, "data": {"trades": [{address, perpsBalance, dayRoe, weekRoe, monthRoe, allTimeRoe, maxDrawdown, winRate, directionBias, lastTrade, allPnl, dayPnl, weekPnl, monthPnl, backtest30Day, tag}]}}`. Числа — строки, ROE и просадка в процентах, 20 строк [LIVE]. Discovery добавляет эти адреса в «поиск» (`discovery.apex_top`). Их отобрали по прошлой прибыли, поэтому в контроль они не попадают.
- Трейдер со скриншота пользователя («30D Backtest 14,334%») — `0xc1a4ecaa0889dd50e839bbea44d2884f7bb0ea31`, он есть в этом списке [LIVE].
- Dextrabot (`app.dextrabot.com`) — другой copy-бот. По описанию на его сайте комиссия копии — 0,055% [3P-UI]. Его документация (`docs.dextrabot.com`) из окружения тоже закрыта.

## 7. Правила торговли, нужные симуляции

| Факт | Значение | Источник | В коде |
|---|---|---|---|
| Минимальный ордер perp | **$10 notional**: ошибка `MinTradeNtl` «Order must have minimum value of $10» | [DOC «Error responses»] | `copy.min_order_usd` |
| Комиссии, базовый тир | тейкер 0,045%, мейкер 0,015% (тир 0); тир — по 14-дневному объёму, спот считается вдвое; **объём субаккаунтов идёт в тир мастера, тир у них общий**; ребейты мейкерам платятся на каждой сделке | [DOC «Fees»] | `costs.taker_fee_bps: 4.5` |
| Комиссия билдера | фронтенд или бот может брать builder fee: на перпах не больше 0,1%, на споте не больше 1%; пользователь одобряет максимум для каждого билдера | [DOC «Builder codes»] | учитывается только если copy-бот её берёт (`copybot_fields.yaml` → `bot.fee_bps`) |
| Funding | платится **каждый час**; формула считает 8-часовую ставку, за час платится 1/8. Базовая процентная часть — 0,01% за 8 ч = 0,00125% в час. Потолок — 4% в час. Платёж = размер позиции × **оракульная** цена × ставка | [DOC «Funding»]; `fundingRate` в `fundingHistory` — часовая ставка, медиана 0,0000125 [LIVE] | платёж = −szi · цена · rate; вместо оракульной цены берём цену свечи (разница — доли процента от платежа) |
| Поддерживающая маржа (MM) | **половина начальной маржи при максимальном плече** актива: от 1,25% (40x) до 16,7% (3x) | [DOC «Margining», «Liquidations»] | `mm_rate = 1 / (2 · maxLeverage)` |
| Ликвидация | когда equity < MM: сначала рыночные ордера в книгу на весь размер; если equity < 2/3 MM и через книгу закрыть не удалось — backstop через liquidator vault, и **MM не возвращается** | [DOC «Liquidations»] | консервативно: при ликвидации теряем и MM |
| Начальная маржа | `размер × mark / плечо`; вывести нереализованный PnL можно, только если остаётся ≥ 10% от notional всех позиций | [DOC «Margining»] | маржа копии = notional / плечо |
| Тик и лот | цена — до 5 значащих цифр и не больше `6 − szDecimals` знаков после запятой (целые цены всегда допустимы); размер — до `szDecimals` знаков | [DOC «Tick and lot size»] | размер копии округляется вниз до `szDecimals` |
| Тиры маржи | у актива есть `marginTableId`, в `meta.marginTables` пороги notional, после которых падает максимальное плечо | [SDK-TS] | для позиций на $50 не влияет, учитываем только базовый `maxLeverage` |
| Unified account и portfolio margin | `userAbstraction`: `unifiedAccount` / `portfolioMargin` / `disabled` / `default`. Залог может лежать в споте, perp-`accountValue` тогда занижает капитал | [SDK-TS][SDK `types.py` Abstraction] | equity берём из окон `portfolio` без префикса `perp` |

## 8. Чего в API нет (и как обходимся)

- **Исторической книги и исторических чужих сделок нет.** Цена «через 10–30 с после сделки кошелька» оценивается по самой мелкой доступной свече (см. §3). Для старой истории к проскальзыванию добавляется штраф задержки, пропорциональный волатильности (`costs.delay_penalty_k`).
- **Исторических плеч и цен ликвидации кошелька нет** (`clearinghouseState` — только текущее состояние). Историческая дистанция до ликвидации оценивается по восстановленным позициям, equity из `portfolio` и MM из `meta`.
- **Семантики полей стороннего copy-бота нет в API Hyperliquid.** Она фиксируется в `copybot_fields.yaml` по документации бота. Пока документации нет, поля помечены `unverified`, и сообщение «НАСТРОЙКИ» не выдаётся.

## 8a. Кошельки-агенты (API wallets) — важно для настройки copy-бота

- API-кошелёк (агент) подписывает действия от имени мастер-аккаунта или его субаккаунтов. Он может торговать, но **не может выводить средства** [DOC «Nonces and API wallets»][2nd].
- Агент удаляется в двух случаях: его срок истёк, или его вытеснили. Безымянного вытесняет одобрение нового безымянного агента, именованного — одобрение агента с тем же именем [DOC «API wallet pruning»]. Вывод для пользователя: если два бота подключены безымянными агентами, второй молча отключит первый. Каждому боту нужно своё имя агента.
- Документация настоятельно советует не использовать адрес удалённого агента повторно: после удаления его nonce-состояние может быть стёрто, и старые подписанные действия можно повторить [DOC]. Для пользователя: новый бот — новый агент.
- Официальный SDK: `Exchange.approve_agent(name=None)`. Без имени создаётся безымянный агент [SDK `exchange.py`].
- Список агентов и срок действия (`validUntil`) публично виден через `extraAgents` [SDK]. `python -m hl_scout account` показывает их и предупреждает, если срок скоро истекает.
- hl_scout ключей не хранит и не создаёт. Всё, что касается агентов, он только **читает** по публичному адресу.

## 9. Чек-лист сверки

Сверено 2026-09-26 (`python -m hl_scout selfcheck` и чтение документации):

- [x] Лимиты выдачи §3 — совпадают с документацией; 1m-свечей ровно 5001 за ~3,5 дня.
- [x] Веса и лимиты §4 — совпадают с документацией. HTTP 429 при превышении — получен на реальном запуске.
- [x] Гранулярность `portfolio`: `day` — 42 точки с шагом ~21 мин; `month` — 91 точка, ~3 ч; `allTime` — ~100 точек (шаг растёт с возрастом аккаунта). Поэтому риск-метрики считаются по часовой MTM-кривой из филлов и свечей, а не по `portfolio`.
- [x] Формат лидерборда §6: 46 867 строк, окна `day`, `week`, `month`, `allTime`. Строка мастера суммирует субаккаунты.
- [x] В `trades` по WS есть поле `users` (`[buyer, seller]`).
- [x] `subscriptionResponse` приходит на каждую подписку.
- [x] `fundingRate` в `fundingHistory` — часовая ставка (24 записи в сутки, медиана 0,0000125).
- [ ] Порядок филлов в `userFillsByTime` на живом адресе. В первом `selfcheck` выбранный адрес (вершина лидерборда) оказался мастером без собственных сделок. Теперь `selfcheck` берёт активный адрес из полосы discovery и считает пустой ответ ошибкой.
- [ ] Для контрагента ликвидации в филле есть `liquidation.liquidatedUser` (детектор ликвидационных ботов). За 7 дней у проверенного адреса ликвидаций не было.

## 10. Что взято из сторонних ботов (просмотрены 2026-09-25)

Просмотрены три публичных торговых бота. В их README заявлены лицензии MIT, MIT и Apache-2.0, но файлов `LICENSE` в репозиториях нет. Поэтому код не копировался: взяты только идеи, реализованные заново.

| Репозиторий (коммит) | Что там есть | Что взяли | Что сознательно НЕ взяли |
|---|---|---|---|
| `SimSimButDifferent/HyperLiquidAlgoBot` (`448d1ef`, JS) | npm-SDK `hyperliquid`; агентский ключ плюс публичный адрес для чтения состояния; ретраи с экспоненциальной паузой | Разделение «адрес для чтения ≠ ключ для подписи» → `resolve_address` через `userRole` | Приватный ключ в `.env`, ордера; лимитер «1 запрос в 10 с» (у нас бюджет по weight) |
| `caelum0x/hyperliqbot` (`09b44e3`, Python) | Копия официального SDK с переписанным WS-менеджером: реестр подписок, переподключение с повторной подпиской, контроль тишины, `get_status()`; именованные агенты на каждого пользователя; лимиты команд Telegram | Сторож тишины, повторная подписка, статус соединения → `WsSession`; идея именованных агентов → совет в §8a; лимит команд и белый список чатов — на этап 2 | Ограничение «5 попыток и сдаться» (monitor должен жить сутками); очередь неотправленных сообщений; хранение ключей пользователей; в репозитории закоммичены `.env` и база пользователей — так делать нельзя |
| `aiwebarchitects/Hyperliquid-Trading-Bot` (`f446467`, Python) | Официальный `hyperliquid-python-sdk`; проверка подключения запросом `user_state` после старта; детект незаполненных ключей с понятной подсказкой; `all_mids` для текущих цен | Проверка подключения до работы → `preflight` (allMids, weight 2) с понятной ошибкой вместо часа повторов; `all_mids` для текущих цен; понятные подсказки при незаполненной настройке | Ключ в `config/api_config.json`, ордера; округление размера `round()` вверх (у нас только вниз) |

Кроме того, из TS SDK `@nktkas/hyperliquid` взяты параметры keep-alive (ping 30 с, pong 10 с) и проверка `subscriptionResponse` (§5).
