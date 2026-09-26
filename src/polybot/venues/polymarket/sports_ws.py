"""Polymarket Sports WS recorder (docs/api_notes.md §10): no subscription, server pings.

Frames are JSON objects with the state of one game. We record them raw and keep a tiny
per-game summary for health output; interpretation (start detection, score changes,
feed latency) happens in analysis.
"""

from __future__ import annotations

import json

from polybot.core.config import SportsWsConfig
from polybot.core.timeutil import now_ns
from polybot.core.ws import ControlEvent, Heartbeat, WsConnection
from polybot.data.records import Kind, Record, RecordWriter, Source


class SportsFeed:
    def __init__(self, cfg: SportsWsConfig, url: str, sink: RecordWriter) -> None:
        self._sink = sink
        self.live_games: set[str] = set()
        self.frames_unparsed = 0
        self.ws = WsConnection(
            name="sports",
            url=url,
            heartbeat=Heartbeat.SERVER_PING,
            on_frame=self._on_frame,
            on_control=self._on_control,
            stale_after_s=cfg.stale_after_s,
            open_timeout_s=cfg.open_timeout_s,
        )

    async def run(self) -> None:
        await self.ws.run()

    def _on_frame(self, text: str, ts_recv_ns: int, conn: WsConnection) -> None:
        record = Record(
            ts_recv_ns=ts_recv_ns,
            source=Source.SPORTS_WS,
            kind=Kind.FRAME,
            payload=text,
            conn_id=conn.conn_id,
        )
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self.frames_unparsed += 1
            record.event_type = "unparsed"
            self._sink.write(record)
            return
        if isinstance(data, dict):
            game_id = data.get("gameId")
            record.key = None if game_id in (None, "") else str(game_id)
            status = data.get("status")
            record.event_type = str(status) if status not in (None, "") else None
            if record.key is not None:
                if data.get("live") is True and data.get("ended") is not True:
                    self.live_games.add(record.key)
                else:
                    self.live_games.discard(record.key)
        self._sink.write(record)

    def _on_control(self, event: ControlEvent, info: dict[str, object], conn: WsConnection) -> None:
        self._sink.write(
            Record(
                ts_recv_ns=now_ns(),
                source=Source.SPORTS_WS,
                kind=Kind.CONTROL,
                event_type=event.value,
                conn_id=conn.conn_id,
                payload=json.dumps(info, default=str),
            )
        )

    def health(self) -> dict[str, object]:
        stats = self.ws.stats
        return {
            "open": self.ws.is_open,
            "frames": stats.frames,
            "reconnects": max(0, stats.connects - 1),
            "stale_closes": stats.stale_closes,
            "live_games": len(self.live_games),
            "frames_unparsed": self.frames_unparsed,
        }
