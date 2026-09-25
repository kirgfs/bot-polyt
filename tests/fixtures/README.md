# Фикстуры тестов: происхождение

Тесты работают без сети. Честно о происхождении данных:

- `gamma_events_tennis.json` — структура и значения полей взяты из **реальных ответов Gamma API**, записанных 2026-08-18 третьей стороной (тестовые фикстуры пакета `polymarket-tennis` 0.1.0, PyPI, MIT): заголовки, slug, теги, формат `gameStartTime` (`"2026-08-18 01:15:00+00"`), значения `sportsMarketType`, строковые JSON-массивы `outcomes`. Поля, которые в той записи были обрезаны (`conditionId`, `clobTokenIds`, `orderPriceMinTickSize`, `orderMinSize`, `secondsDelay`, `gameId`, `sportsradarMatchId`), добавлены **синтетически** по именам из кода SDK (`docs/api_notes.md` §11). Обёртка `{"events": [...], "next_cursor": null}` — формат `/events/keyset` по коду SDK.
- Кадры market WS и Sports WS в тестах собираются кодом по форматам из SDK и `Polymarket/agent-skills` (`docs/api_notes.md` §10). Это не записи реальной сессии: записанные сессии появятся после запуска рекордера на VPS и заменят синтетику в контрактных тестах.
- Ответы OddsPapi собраны по фрагментам их документации из поисковой выдачи ([2nd], `docs/data_sources.md` §5).
