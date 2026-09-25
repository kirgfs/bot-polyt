"""Configuration: secrets and mode from `.env`, everything else from `config/*.yaml`.

Only our own parameters live in YAML. Exchange constants (ticks, fees, delays, rewards,
start times) are read from the API at runtime (CLAUDE.md, rule 3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SportName = Literal["tennis", "soccer", "basketball"]


class Settings(BaseSettings):
    """Environment settings. Secrets are SecretStr so they never render in logs or repr."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mode: Literal["paper", "shadow", "live"] = Field(default="paper", alias="MODE")
    live_trading: bool = Field(default=False, alias="LIVE_TRADING")

    polymarket_private_key: SecretStr | None = Field(default=None, alias="POLYMARKET_PRIVATE_KEY")
    clob_api_key: SecretStr | None = Field(default=None, alias="CLOB_API_KEY")
    clob_secret: SecretStr | None = Field(default=None, alias="CLOB_SECRET")
    clob_passphrase: SecretStr | None = Field(default=None, alias="CLOB_PASSPHRASE")
    oddspapi_api_key: SecretStr | None = Field(default=None, alias="ODDSPAPI_API_KEY")

    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    config_dir: Path = Field(default=Path("./config"), alias="CONFIG_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: Literal["json", "console"] = Field(default="json", alias="LOG_FORMAT")

    @field_validator(
        "polymarket_private_key",
        "clob_api_key",
        "clob_secret",
        "clob_passphrase",
        "oddspapi_api_key",
        mode="before",
    )
    @classmethod
    def _empty_secret_is_none(cls, value: object) -> object:
        return None if value == "" else value


class LiveTradingNotAllowedError(RuntimeError):
    pass


def assert_live_trading_allowed(settings: Settings) -> None:
    """Gate for any code path that sends a real order (CLAUDE.md, rule 1).

    The second half of the gate, explicit confirmation of the launch in chat,
    cannot be checked by code; this only enforces the `.env` part.
    """
    if settings.mode != "live" or not settings.live_trading:
        raise LiveTradingNotAllowedError(
            "real orders are disabled: need MODE=live and LIVE_TRADING=true in .env "
            "and an explicit confirmation of the launch in chat"
        )


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- config/base.yaml


class GeoblockConfig(_Strict):
    url: str = "https://polymarket.com/api/geoblock"
    # ISO 3166-1 alpha-2 codes where the operator is allowed to run the bot.
    allowed_countries: tuple[str, ...]
    interval_s: float = 600.0
    timeout_s: float = 10.0
    # Periodic check only: consecutive network errors tolerated before stopping.
    max_consecutive_errors: int = 3

    @field_validator("allowed_countries")
    @classmethod
    def _upper(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("allowed_countries must not be empty")
        return tuple(code.strip().upper() for code in value)


class PolymarketEndpoints(_Strict):
    # docs/api_notes.md §2 ([SDK] polymarket/environments.py)
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    sports_ws_url: str = "wss://sports-api.polymarket.com/ws"


class HttpConfig(_Strict):
    timeout_s: float = 15.0
    user_agent: str = "polybot-recorder/0.1"


class BaseConfig(_Strict):
    geoblock: GeoblockConfig
    polymarket: PolymarketEndpoints = PolymarketEndpoints()
    http: HttpConfig = HttpConfig()


# ------------------------------------------------------------ config/recorder.yaml


class SportConfig(_Strict):
    tag_slugs: tuple[str, ...]
    # Tags every listed event must also carry (Gamma `tag_match=all`), e.g. `games`.
    require_tag_slugs: tuple[str, ...] = ()
    # Gamma sportsMarketType values whose order books we record (docs/api_notes.md §11).
    market_types: tuple[str, ...]
    exclude_doubles: bool = False


class DiscoveryConfig(_Strict):
    interval_s: float = 120.0
    page_size: Annotated[int, Field(ge=1, le=500)] = 100
    max_pages: int = 100
    # WS books are recorded for games starting within this horizon...
    subscribe_horizon_h: float = 72.0
    # ...and kept after the scheduled start (live phase, exchange auto-cancel, early starts).
    live_lookback_h: float = 6.0
    # Gamma snapshots cover a wider horizon for listing/volume statistics.
    track_horizon_h: float = 168.0
    # All tracked events are written this often and at the first poll of each UTC day
    # (every date partition is self-contained); in between, only structural changes.
    full_snapshot_interval_s: float = 6 * 3600.0
    # Cap on markets with WS books at once; the nearest to their start are kept.
    max_subscribed_markets: Annotated[int, Field(ge=1)] = 1500


class MarketWsConfig(_Strict):
    # No published cap; 200 follows NautilusTrader's default (docs/api_notes.md §10, [3P]).
    max_assets_per_conn: Annotated[int, Field(ge=1, le=1000)] = 200
    custom_feature_enabled: bool = False
    ping_interval_s: float = 10.0
    stale_after_s: float = 30.0
    open_timeout_s: float = 10.0
    snapshot_timeout_s: float = 10.0
    max_frame_bytes: int = 16 * 1024 * 1024
    resync_min_interval_s: float = 30.0


class SportsWsConfig(_Strict):
    enabled: bool = True
    stale_after_s: float = 30.0
    open_timeout_s: float = 10.0


class RestValidationConfig(_Strict):
    enabled: bool = True
    interval_s: float = 10.0
    batch_size: Annotated[int, Field(ge=1, le=100)] = 25


class ClobMetaConfig(_Strict):
    clob_markets_refresh_s: float = 6 * 3600.0
    rewards_interval_s: float = 3600.0
    rewards_max_pages: int = 200


class ProbeConfig(_Strict):
    rest_interval_s: float = 60.0


class SinkConfig(_Strict):
    # Flush on whichever comes first (data/sink.py): time, buffered rows, buffered payload.
    flush_interval_s: Annotated[float, Field(gt=0)] = 10.0
    flush_rows: Annotated[int, Field(ge=1)] = 20_000
    flush_mb: Annotated[float, Field(gt=0)] = 8.0
    # Hard cap if the disk fails or lags: oldest rows are dropped and counted, not kept.
    max_buffer_mb: Annotated[float, Field(gt=0)] = 64.0
    max_buffer_rows: Annotated[int, Field(ge=1)] = 500_000
    compression_level: int = 6


class OddsPapiWsConfig(_Strict):
    url: str = "wss://api.oddspapi.io/v4/ws"
    # Exact login/subscribe frames are not published in sources we could read
    # (docs/data_sources.md §5): set them from the vendor docs on the VPS.
    # "{api_key}" is substituted at runtime and never logged.
    login_message: str | None = None
    subscribe_messages: tuple[str, ...] = ()


class OddsPapiPaidRestConfig(_Strict):
    fixtures_interval_s: float = 6 * 3600.0
    # Poll cadence for odds-by-tournaments by time to the nearest start in the tournament.
    odds_interval_far_s: float = 1800.0
    odds_interval_near_s: float = 60.0
    near_window_h: float = 3.0
    fixtures_window_days: Annotated[int, Field(ge=1, le=10)] = 7


class OddsPapiConfig(_Strict):
    mode: Literal["off", "paid_rest", "ws"] = "off"
    base_url: str = "https://api.oddspapi.io/v4"
    # Vendor cooldown is ~0.88-1 s per endpoint on free (docs/data_sources.md §5).
    min_interval_s: float = 1.1
    monthly_request_budget: int = 250
    daily_request_budget: int = 40
    sport_ids: dict[SportName, int] = {"tennis": 12, "soccer": 10, "basketball": 11}
    # Match-winner market id per sport; None = unknown, resolve via /markets on the VPS.
    winner_market_ids: dict[SportName, int | None] = {
        "tennis": 171,
        "soccer": 101,
        "basketball": None,
    }
    bookmakers: tuple[str, ...] = ("pinnacle",)
    paid_rest: OddsPapiPaidRestConfig = OddsPapiPaidRestConfig()
    ws: OddsPapiWsConfig = OddsPapiWsConfig()


class HealthConfig(_Strict):
    status_interval_s: float = 30.0
    # Process memory (RSS) goes to the status file every interval and to the log this often.
    memory_log_interval_s: float = 60.0
    # Above this anonymous RSS the memory log line becomes a warning.
    rss_warn_mb: float = 250.0


class DailyConfig(_Strict):
    """`polybot daily`: reports for finished UTC days and raw-data retention on the VPS."""

    # Raw partitions of this many most recent finished days stay on disk. Older days are
    # deleted, and only once their daily report has been built.
    keep_raw_days: Annotated[int, Field(ge=0)] = 2
    # Sources kept past retention: tiny, and matching needs fixtures from earlier days.
    keep_sources: tuple[str, ...] = ("oddspapi_rest",)
    # DuckDB limits for reports in the tools container; overflow spills to data/tmp.
    # Measured on a synthetic day of 4.9M rows: 128 MB / 1 thread → peak RSS 362 MB, 60 s;
    # 256 MB → 505 MB at the same speed; 2 threads at 128 MB run out of memory.
    duckdb_memory_mb: Annotated[int, Field(ge=64)] = 128
    duckdb_threads: Annotated[int, Field(ge=1)] = 1


class RecorderConfig(_Strict):
    sports: dict[SportName, SportConfig]
    discovery: DiscoveryConfig = DiscoveryConfig()
    market_ws: MarketWsConfig = MarketWsConfig()
    sports_ws: SportsWsConfig = SportsWsConfig()
    rest_validation: RestValidationConfig = RestValidationConfig()
    clob_meta: ClobMetaConfig = ClobMetaConfig()
    probes: ProbeConfig = ProbeConfig()
    sink: SinkConfig = SinkConfig()
    oddspapi: OddsPapiConfig = OddsPapiConfig()
    health: HealthConfig = HealthConfig()
    daily: DailyConfig = DailyConfig()


class AppConfig(_Strict):
    base: BaseConfig
    recorder: RecorderConfig


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return loaded


def load_config(config_dir: Path) -> AppConfig:
    return AppConfig(
        base=BaseConfig.model_validate(_read_yaml(config_dir / "base.yaml")),
        recorder=RecorderConfig.model_validate(_read_yaml(config_dir / "recorder.yaml")),
    )
