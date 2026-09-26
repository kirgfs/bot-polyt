"""OddsPapi client (docs/data_sources.md §5): REST with a persistent request budget, raw WS.

Every response is stored raw (`source=oddspapi_rest`). The API key travels only as the
`apiKey` query parameter (vendor convention) and is never written to Parquet or logs:
the recorded endpoint string is built from the parameters without it.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from polybot.core.config import OddsPapiConfig
from polybot.core.http import TimedResponse, timed_request
from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S, mono_ns, now_ns, ns_to_datetime
from polybot.core.ws import ControlEvent, Heartbeat, WsConnection
from polybot.data.records import Kind, Record, RecordWriter, Source

log = get_logger(__name__)

_QUOTA_HEADER_HINTS = ("limit", "remaining", "quota", "credit", "reset", "retry", "usage")


class OddsPapiConfigError(RuntimeError):
    pass


@dataclass
class BudgetState:
    month: str = ""
    month_count: int = 0
    day: str = ""
    day_count: int = 0


class RequestBudget:
    """Monthly and daily request caps, persisted so restarts do not reset them."""

    def __init__(self, path: Path, monthly: int, daily: int) -> None:
        self._path = path
        self._monthly = monthly
        self._daily = daily
        self.state = self._load()

    def _load(self) -> BudgetState:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return BudgetState()
        return BudgetState(
            month=str(data.get("month", "")),
            month_count=int(data.get("month_count", 0)),
            day=str(data.get("day", "")),
            day_count=int(data.get("day_count", 0)),
        )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state.__dict__), encoding="utf-8")
        os.replace(tmp, self._path)

    def _roll(self, ts_ns: int) -> None:
        stamp = ns_to_datetime(ts_ns)
        month, day = stamp.strftime("%Y-%m"), stamp.strftime("%Y-%m-%d")
        if self.state.month != month:
            self.state.month, self.state.month_count = month, 0
        if self.state.day != day:
            self.state.day, self.state.day_count = day, 0

    def remaining(self, ts_ns: int | None = None) -> tuple[int, int]:
        self._roll(ts_ns or now_ns())
        return (
            max(0, self._monthly - self.state.month_count),
            max(0, self._daily - self.state.day_count),
        )

    def try_consume(self, ts_ns: int | None = None) -> bool:
        month_left, day_left = self.remaining(ts_ns)
        if month_left <= 0 or day_left <= 0:
            return False
        self.state.month_count += 1
        self.state.day_count += 1
        self._save()
        return True


class OddsPapiClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        cfg: OddsPapiConfig,
        api_key: SecretStr | None,
        sink: RecordWriter,
        budget: RequestBudget,
    ) -> None:
        if api_key is None:
            raise OddsPapiConfigError("ODDSPAPI_API_KEY is not set in .env")
        self._http = http
        self._cfg = cfg
        self._key = api_key
        self._sink = sink
        self.budget = budget
        self._last_call_mono: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self.requests = 0
        self.errors = 0

    async def get(
        self, path: str, params: dict[str, Any] | None = None, *, key: str | None = None
    ) -> TimedResponse | None:
        """GET with per-endpoint cooldown and budget. None when the budget is exhausted."""
        params = dict(params or {})
        async with self._lock:
            wait_ns = (
                self._last_call_mono.get(path, -(10**18))
                + int(self._cfg.min_interval_s * NS_PER_S)
                - mono_ns()
            )
            if wait_ns > 0:
                await asyncio.sleep(wait_ns / NS_PER_S)
            if not self.budget.try_consume():
                log.warning(
                    "oddspapi_budget_exhausted", path=path, state=self.budget.state.__dict__
                )
                return None
            self._last_call_mono[path] = mono_ns()
        endpoint = path + ("?" + urlencode(sorted(params.items())) if params else "")
        self.requests += 1
        try:
            response = await timed_request(
                self._http,
                "GET",
                self._cfg.base_url.rstrip("/") + path,
                params={**params, "apiKey": self._key.get_secret_value()},
            )
        except httpx.HTTPError as exc:
            self.errors += 1
            self._sink.write(
                Record(
                    ts_recv_ns=now_ns(),
                    source=Source.ODDSPAPI_REST,
                    kind=Kind.CONTROL,
                    event_type="request_error",
                    endpoint=endpoint,
                    payload=json.dumps({"error": type(exc).__name__}),
                )
            )
            return None
        self._sink.write(
            Record(
                ts_recv_ns=response.ts_recv_ns,
                source=Source.ODDSPAPI_REST,
                kind=Kind.REST,
                event_type=path.strip("/"),
                key=key,
                endpoint=endpoint,
                status=response.status,
                latency_ns=response.latency_ns,
                payload=response.text,
            )
        )
        quota = {
            name: value
            for name, value in response.headers.items()
            if any(hint in name.lower() for hint in _QUOTA_HEADER_HINTS)
        }
        if quota:
            self._sink.write(
                Record(
                    ts_recv_ns=response.ts_recv_ns,
                    source=Source.ODDSPAPI_REST,
                    kind=Kind.CONTROL,
                    event_type="quota_headers",
                    endpoint=endpoint,
                    payload=json.dumps(quota),
                )
            )
        if not response.ok:
            self.errors += 1
            log.warning("oddspapi_http_error", path=path, status=response.status)
        return response


class OddsPapiWsRecorder:
    """Raw frames of the vendor WS. Login/subscribe frames come from config (unknown format)."""

    def __init__(self, cfg: OddsPapiConfig, api_key: SecretStr | None, sink: RecordWriter) -> None:
        if api_key is None:
            raise OddsPapiConfigError("ODDSPAPI_API_KEY is not set in .env")
        if not cfg.ws.login_message and not cfg.ws.subscribe_messages:
            raise OddsPapiConfigError(
                "oddspapi.mode=ws needs ws.login_message/subscribe_messages in recorder.yaml"
            )
        self._cfg = cfg
        self._key = api_key
        self._sink = sink
        self.ws = WsConnection(
            name="oddspapi-ws",
            url=cfg.ws.url.replace("{api_key}", api_key.get_secret_value()),
            heartbeat=Heartbeat.NONE,
            on_frame=self._on_frame,
            on_open=self._on_open,
            on_control=self._on_control,
            stale_after_s=120.0,
        )

    async def run(self) -> None:
        await self.ws.run()

    async def _on_open(self, conn: WsConnection) -> None:
        secret = self._key.get_secret_value()
        messages = [self._cfg.ws.login_message] if self._cfg.ws.login_message else []
        messages.extend(self._cfg.ws.subscribe_messages)
        for message in messages:
            await conn.send(message.replace("{api_key}", secret))

    def _on_frame(self, text: str, ts_recv_ns: int, conn: WsConnection) -> None:
        record = Record(
            ts_recv_ns=ts_recv_ns,
            source=Source.ODDSPAPI_WS,
            kind=Kind.FRAME,
            payload=text,
            conn_id=conn.conn_id,
        )
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            channel, kind = data.get("channel"), data.get("type")
            record.event_type = ":".join(str(x) for x in (channel, kind) if x) or None
            payload = data.get("payload")
            fixture = data.get("fixtureId") or (
                payload.get("fixtureId") if isinstance(payload, dict) else None
            )
            record.key = None if fixture in (None, "") else str(fixture)
        self._sink.write(record)

    def _on_control(self, event: ControlEvent, info: dict[str, object], conn: WsConnection) -> None:
        self._sink.write(
            Record(
                ts_recv_ns=now_ns(),
                source=Source.ODDSPAPI_WS,
                kind=Kind.CONTROL,
                event_type=event.value,
                conn_id=conn.conn_id,
                payload=json.dumps(info, default=str),
            )
        )
