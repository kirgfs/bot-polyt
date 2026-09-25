# Hyperliquid: заметки по API для hl_scout

> Сбор: **2026-09-25**. Правило проекта: в коде нет «знаний по памяти». Каждый факт об API сначала записан здесь со ссылкой и меткой доверия, потом используется в коде. Пункты с меткой **[DOC-S]** и ниже сверить при первом запуске с сетью (чек-лист в §9).

## Как собраны данные и насколько им верить

Из облачного окружения, где писался код, закрыты `hyperliquid.gitbook.io`, `api.hyperliquid.xyz`, `stats-data.hyperliquid.xyz` и `api.telegram.org` (egress 403). Поэтому источники такие:

| Метка | Источник | Доверие |
|---|---|---|
| **[SDK]** | Официальный Python SDK `hyperliquid-dex/hyperliquid-python-sdk`, коммит `2fdb18f` (2026-06-04): `hyperliquid/info.py`, `api.py`, `websocket_manager.py`, `utils/types.py`, `utils/constants.py` | Высокое: так ходит сам SDK биржи |
| **[SDK-TS]** | TypeScript SDK `@nktkas/hyperliquid` 0.33.3 (npm). Типы ответов с комментариями и ссылками на разделы официальной документации | Среднее-высокое: сообщество, но схемы подробные и со ссылками на docs |
| **[DOC-S]** | Официальная документация `hyperliquid.gitbook.io`, прочитанная через выдержки поисковика (сама страница закрыта) | Среднее: **сверить** |
| **[3P]** | Сторонний код: `cordilleradev/hyperliquid-go` | Низкое-среднее |
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
| `portfolio` | `user` | Массив пар `[период, {accountValueHistory: [[мс, "число"]], pnlHistory: [[мс, "число"]], vlm}]`, периоды: `day`, `week`, `month`, `allTime`, `perpDay`, `perpWeek`, `perpMonth`, `perpAllTime` | [SDK метод][SDK-TS тип][DOC-S] |
| `userNonFundingLedgerUpdates` | `user`, `startTime`, `endTime` | `{time, hash, delta}`; `delta.type`: `deposit`, `withdraw`, `internalTransfer` (`user`, `destination`), `subAccountTransfer` (`user`, `destination`), `spotTransfer`, `send` (`user`, `destination`, `usdcValue`), `accountClassTransfer`, `liquidation` (`liquidatedNtlPos`, `accountValue`, `leverageType`, `liquidatedPositions`), `vaultCreate`, `vaultDeposit`, `vaultWithdraw`, `vaultDistribution`, `rewardsClaim` и др. | [SDK][SDK-TS] |
| `userFunding` | `user`, `startTime`, `endTime` | `{time, hash, delta{type: "funding", coin, usdc, szi, fundingRate, nSamples}}` | [SDK][SDK-TS] |
| `fundingHistory` | `coin`, `startTime`, `endTime` | `{coin, fundingRate, premium, time}` | [SDK][SDK-TS] |
| `candleSnapshot` | `{"req": {coin, interval, startTime, endTime}}` | `t` (открытие), `T` (закрытие), `s`, `i`, `o`, `h`, `l`, `c` (строки), `v`, `n` | [SDK][SDK-TS] |
| `metaAndAssetCtxs` | — | `[meta, ctxs]`: `meta.universe[]` = `name`, `szDecimals`, `maxLeverage`, `marginTableId`, `onlyIsolated?`, `isDelisted?`, `marginMode?`; `meta.marginTables`; `ctxs[]` = `markPx`, `midPx`, `oraclePx`, `funding`, `dayNtlVlm`, `openInterest`, `prevDayPx` (порядок совпадает с `universe`) | [SDK docstring][SDK-TS] |
| `spotMetaAndAssetCtxs` | — | Спот-пары, токены и цены — для оценки спот-балансов в детекторе хеджей | [SDK] |
| `userRole` | `user` | `role`: `missing` / `user` / `agent` / `vault` / `subAccount` (для `subAccount` есть `data.master`) | [SDK метод][SDK-TS][DOC-S] |

### Имена монет в филлах
- Основные перпы — `BTC`, `ETH`, ...
- Спот — `@<index>` или `PURR/USDC` [SDK `info.py`: спот-активы и имена пар].
- Перпы builder-deployed DEX (HIP-3) — `<dex>:<COIN>`, например `xyz:AAPL` [SDK-TS `perpDexs`; SDK пример `test:ABC`].
- hl_scout копирует только основные перпы. Спот-филлы идут только в детектор хеджей. Филлы HIP-3 считаются неповторимыми (настройка `universe.allow_hip3`).

## 3. Лимиты выдачи и пагинация

- `userFillsByTime`: **не больше 2000 филлов за ответ; доступны только 10 000 последних филлов** [DOC-S]. Следствие: у очень активных кошельков 90 дней истории может не быть. Такой кошелёк помечается `history_truncated` и не проходит фильтр «90 дней» (fail-closed).
- Запросы с диапазоном времени (`fundingHistory`, `userFunding`, `userNonFundingLedgerUpdates`) возвращают **не больше 500 элементов**. Дальше листаем: `startTime` = время последнего элемента [DOC-S].
- `candleSnapshot`: **доступны только последние 5000 свечей** каждого интервала [DOC-S]. Интервалы: `1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d 3d 1w 1M` [SDK-TS][DOC-S]. Следствие для бэктеста:

  | Интервал | Глубина |
  |---|---|
  | 1m | ≈ 3,5 дня |
  | 15m | ≈ 52 дня |
  | 1h | ≈ 208 дней |

  Цена внутри длинной свечи интерполируется, поэтому точность задержки 10–30 с на старой истории ниже (см. `docs/methodology.md`).

## 4. Rate limits

| Правило | Значение | Источник |
|---|---|---|
| Общий бюджет REST | **1200 weight в минуту на IP** | [DOC-S] |
| weight 2 | `l2Book`, `allMids`, `clearinghouseState`, `orderStatus`, `spotClearinghouseState`, `exchangeStatus` | [DOC-S] |
| weight 60 | `userRole` | [DOC-S] |
| weight 20 | все остальные документированные info-запросы | [DOC-S] |
| Доплата за объём | +1 за каждые 20 элементов ответа: `userFills`, `userFillsByTime`, `fundingHistory`, `userFunding`, `historicalOrders`, `recentTrades` и др.; `candleSnapshot` — +1 за каждые 60 свечей | [DOC-S] |
| WebSocket | не больше 1000 подписок; **не больше 10 уникальных пользователей** в пользовательских подписках (`userFills` и т. п.); не больше 30 новых соединений в минуту; не больше 2000 исходящих сообщений в минуту | [DOC-S][2nd] |

В коде: token bucket по weight с запасом (`api.weight_budget_per_min`, по умолчанию 1000 из 1200). Доплата за объём списывается после ответа. Для лидерборда (другой хост) лимит неизвестен, поэтому запрашиваем редко и кэшируем.

## 5. WebSocket

- Подписка: `{"method": "subscribe", "subscription": {...}}`, отписка — `"unsubscribe"` [SDK].
- Ping: `{"method": "ping"}`, ответ — канал `pong`. SDK шлёт ping каждые 50 с [SDK].
- `{"type": "trades", "coin": "BTC"}` → канал `trades`, `data` = список сделок `{coin, side, px, sz, time, hash, tid, users: [покупатель, продавец]}` [SDK][SDK-TS: `users` — «addresses of the two users involved in the trade [buyer, seller]»]. Этим каналом discovery собирает адреса из крупных сделок.
- `{"type": "userFills", "user": ...}` → канал `userFills`, `data = {user, isSnapshot, fills[]}` [SDK `types.py`]. Нужен monitor-у (следующий этап).

## 6. Лидерборд (неофициальный эндпоинт)

- `GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` → `{"leaderboardRows": [{ethAddress, accountValue (строка), windowPerformances: [[окно, {pnl, roi, vlm}], ...], prize, displayName}]}`. Окна: `day`, `week`, `month`, `allTime` [3P: `hyperliquid-go`, `types/leaderboard.go` и `rest/api.go`][2nd].
- Эндпоинт **не описан** в документации публичного API: им пользуется веб-интерфейс лидерборда. Поэтому:
  - окна разбираем по имени, а не по индексу;
  - на отсутствующие поля код не падает;
  - если эндпоинт недоступен, discovery работает дальше на адресах из крупных сделок и ручном списке.
- Правила попадания в лидерборд неизвестны, в том числе остаются ли там «умершие» аккаунты. Это источник остаточного survivorship bias (см. `docs/methodology.md` §6).

## 7. Правила торговли, нужные симуляции

| Факт | Значение | Источник | В коде |
|---|---|---|---|
| Минимальный ордер perp | **$10 notional** («Order must have minimum value of $10») | [DOC-S: error responses][2nd] | `copy.min_order_usd` |
| Комиссии, базовый тир | тейкер 0,045%, мейкер 0,015%; тиры по 14-дневному объёму | [2nd, несколько источников] | `costs.taker_fee_bps: 4.5` |
| Funding | платится **каждый час**; формула считает 8-часовую ставку, за час платится 1/8. Базовая процентная часть — 0,01% за 8 ч = 0,00125% в час | [DOC-S] | `fundingHistory.fundingRate` применяется к позиции как часовая ставка: платёж = −szi · цена · rate |
| Поддерживающая маржа (MM) | **половина начальной маржи при максимальном плече** актива: от 1,25% (40x) до 16,7% (3x) | [DOC-S] | `mm_rate = 1 / (2 · maxLeverage)` |
| Ликвидация | когда equity < MM: сначала рыночные ордера в книгу, при backstop-ликвидации MM не возвращается | [DOC-S] | консервативно: при ликвидации теряем и MM |
| Тиры маржи | у актива есть `marginTableId`, в `meta.marginTables` пороги notional, после которых падает максимальное плечо | [SDK-TS] | для позиций на $50 не влияет, учитываем только базовый `maxLeverage` |
| Unified account и portfolio margin | `userAbstraction`: `unifiedAccount` / `portfolioMargin` / `disabled` / `default`. Залог может лежать в споте, perp-`accountValue` тогда занижает капитал | [SDK-TS][SDK `types.py` Abstraction] | equity берём из окон `portfolio` без префикса `perp` |

## 8. Чего в API нет (и как обходимся)

- **Исторической книги и исторических чужих сделок нет.** Цена «через 10–30 с после сделки кошелька» оценивается по самой мелкой доступной свече (см. §3). Для старой истории к проскальзыванию добавляется штраф задержки, пропорциональный волатильности (`costs.delay_penalty_k`).
- **Исторических плеч и цен ликвидации кошелька нет** (`clearinghouseState` — только текущее состояние). Историческая дистанция до ликвидации оценивается по восстановленным позициям, equity из `portfolio` и MM из `meta`.
- **Семантики полей стороннего copy-бота нет в API Hyperliquid.** Она фиксируется в `copybot_fields.yaml` по документации бота. Пока документации нет, поля помечены `unverified`, и сообщение «НАСТРОЙКИ» не выдаётся.

## 9. Чек-лист сверки (при первом запуске с сетью)

- [ ] Порядок филлов в `userFillsByTime` (по возрастанию времени?) и поведение при 2000/10 000 — тест `scripts`/`hl_scout selfcheck`.
- [ ] Гранулярность `accountValueHistory` в окнах `allTime`, `month`, `week`.
- [ ] Формат лидерборда (§6) и примерное число строк.
- [ ] Есть ли в `trades` по WS поле `users`.
- [ ] Для контрагента ликвидации в филле есть `liquidation.liquidatedUser` (детектор ликвидационных ботов).
- [ ] `fundingRate` в `fundingHistory` — часовая ставка (типичное значение ≈ 0.0000125).
- [ ] Лимиты §4: ответы 429 при превышении.

Команда `python -m hl_scout selfcheck` проверяет эти пункты на живом API и пишет результат в лог.
