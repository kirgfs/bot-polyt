"""Configuration: config.yaml (thresholds) + copybot_fields.yaml (copy-bot semantics) + .env (secrets)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DepositCfg(_Model):
    total_usd: float = 50.0
    max_wallets: int = 2
    compare_split: bool = True


class GoalCfg(_Model):
    target_usd: float = 1000.0
    horizon_days: int = 30
    levels_usd: list[float] = [100.0, 1000.0]


class CopyCfg(_Model):
    delays_s: list[float] = [10.0, 30.0]
    min_order_usd: float = 10.0
    human_reaction_min: float = 60.0
    aggregate_window_ms: int = 2000

    @property
    def delay_s(self) -> float:
        """Worst-case delay used for the main backtest."""
        return max(self.delays_s)


class SlippageTier(_Model):
    min_day_volume_usd: float
    bps: float


class CostsCfg(_Model):
    taker_fee_bps: float = 4.5
    slippage_bps_tiers: list[SlippageTier] = [
        SlippageTier(min_day_volume_usd=500e6, bps=2),
        SlippageTier(min_day_volume_usd=50e6, bps=4),
        SlippageTier(min_day_volume_usd=5e6, bps=8),
        SlippageTier(min_day_volume_usd=0, bps=20),
    ]
    delay_penalty_k: float = 0.5
    stop_slippage_pct: float = 0.5

    def slippage_bps(self, day_volume_usd: float | None) -> float:
        vol = day_volume_usd or 0.0
        for tier in sorted(self.slippage_bps_tiers, key=lambda t: -t.min_day_volume_usd):
            if vol >= tier.min_day_volume_usd:
                return tier.bps
        return max(t.bps for t in self.slippage_bps_tiers)


class UniverseCfg(_Model):
    allow_hip3: bool = False
    exclude_coins: list[str] = []


class LargeTradesCfg(_Model):
    enabled: bool = True
    listen_min: float = 10.0
    min_notional_usd: float = 250_000.0
    top_coins: int = 15


class TtlCfg(_Model):
    leaderboard_h: float = 12
    portfolio_h: float = 6
    state_min: float = 30
    meta_h: float = 6
    role_days: float = 7
    ledger_h: float = 6


class DiscoveryCfg(_Model):
    leaderboard_url: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
    use_leaderboard: bool = True
    pool_min_month_volume_usd: float = 250_000
    pool_min_alltime_volume_usd: float = 2_000_000
    pool_max: int = 400
    deep_max: int = 150
    stage1_relax: float = 1.3
    large_trades: LargeTradesCfg = LargeTradesCfg()
    history_days: int = 180
    manual_addresses: list[str] = []
    ttl: TtlCfg = TtlCfg()


class MartingaleCfg(_Model):
    adverse_pct: float = 0.01
    max_trip_share: float = 0.15
    max_loss_adds_in_trip: int = 2


class MarketMakerCfg(_Model):
    maker_share: float = 0.80
    fills_per_day: float = 100


class LiqBotCfg(_Model):
    counterparty_share: float = 0.20


class HedgeCfg(_Model):
    spot_vs_perp_share: float = 0.30
    opposite_time_share: float = 0.30
    opposite_ratio: float = 0.50
    funding_pnl_share: float = 0.40


class ClusterCfg(_Model):
    sync_window_s: float = 30
    sync_share: float = 0.50
    min_matches: int = 10
    max_counterparty_degree: int = 3


class FiltersCfg(_Model):
    lookback_days: int = 90
    min_trades: int = 50
    active_within_days: float = 7
    min_avg_hold_min: float = 15
    max_drawdown: float = 0.30
    months_window: int = 3
    min_profitable_months: int = 2
    max_top3_share: float = 0.50
    equity_min_usd: float = 2_000
    equity_max_usd: float = 200_000
    max_typical_concurrent: float = 3
    coins_min: int = 1
    coins_max: int = 5
    coin_min_share: float = 0.05
    min_liq_distance: float = 0.10
    max_liquidations: int = 0
    min_fill_coverage: float = 0.8
    martingale: MartingaleCfg = MartingaleCfg()
    market_maker: MarketMakerCfg = MarketMakerCfg()
    liq_bot: LiqBotCfg = LiqBotCfg()
    hedge: HedgeCfg = HedgeCfg()
    cluster: ClusterCfg = ClusterCfg()


class ScoreWeights(_Model):
    skill: float = 0.30
    stability: float = 0.25
    copyability: float = 0.30
    drawdown: float = 0.15


class ScorePenalties(_Model):
    small_adds: float = 0.10
    weekly_spike: float = 0.15
    short_history: float = 0.10


class ScoreCfg(_Model):
    weights: ScoreWeights = ScoreWeights()
    penalties: ScorePenalties = ScorePenalties()
    short_history_days: float = 120
    weekly_spike_z: float = 2.5
    bootstrap_samples: int = 2000
    bootstrap_block_days: int = 5


class RecommendCfg(_Model):
    min_dsr: float = 0.50
    min_profitable_test_share: float = 0.60
    min_test_windows: int = 4
    max_p_ruin: float = 0.10
    max_lost_actions: float = 0.20
    max_margin_share: float = 0.80
    liq_safety: float = 1.5
    max_stop_in_vain: float = 0.30
    max_cost_share: float = 0.40


class GridCfg(_Model):
    target_position_usd: list[float] = [12, 15, 20, 30, 40, 60, 80, 120]
    leverage: list[int] = [1, 2, 3, 5, 10]
    max_trade_mult: float = 1.5
    buy_times: list[int] = [1, 2, 0]
    small_size: list[Literal["skip", "buy"]] = ["skip", "buy"]
    price_sl: list[Literal["none", "mae"]] = ["none", "mae"]


class ProfileLimits(_Model):
    max_p_ruin: float
    max_p_loss: float
    max_leverage: int = 10
    max_exposure: float = 4.0  # max number of positions × position notional / deposit


class BacktestCfg(_Model):
    train_days: int = 60
    test_days: int = 14
    step_days: int = 14
    grid: GridCfg = GridCfg()
    sl_mae_mult: float = 1.1
    profiles: dict[str, ProfileLimits] = Field(
        default_factory=lambda: {
            "conservative": ProfileLimits(max_p_ruin=0.02, max_p_loss=0.10, max_leverage=3, max_exposure=1.0),
            "medium": ProfileLimits(max_p_ruin=0.05, max_p_loss=0.20, max_leverage=5, max_exposure=2.0),
            "aggressive": ProfileLimits(max_p_ruin=0.10, max_p_loss=0.35, max_leverage=10, max_exposure=4.0),
        }
    )
    tune_mc_paths: int = 2000


class MonteCarloCfg(_Model):
    paths: int = 10_000
    block_days: int = 3
    ruin_usd: float = 5.0
    loss_threshold: float = 0.40
    seed: int = 20260925


class RegimeCfg(_Model):
    enabled: bool = True
    reference_coin: str = "BTC"
    vol_window_h: int = 24
    extreme_quantile: float = 0.95


class RulesCfg(_Model):
    copy_dd_stop: float = 0.20
    copy_dd_grid: list[float] = [0.15, 0.20, 0.30]
    wallet_dd7_mult: float = 1.5
    wallet_dd7_grid: list[float] = [1.25, 1.5, 2.0]
    wallet_dd7_min: float = 0.05
    consecutive_losses: int = 5
    consecutive_losses_grid: list[int] = [4, 5, 7]
    style_mult: float = 2.0
    style_min_trades: int = 5
    new_coin_share: float = 0.50
    near_liq: float = 0.05
    inactive_days: float = 5
    withdrawal_share: float = 0.30
    warn_fraction: float = 0.65
    regime: RegimeCfg = RegimeCfg()
    replace_score_gap: float = 10


class ProjectCfg(_Model):
    loss_ceiling_usd: float = 20
    withdraw_at_multiple: float = 2.0
    paper_days: int = 14
    my_copy_account: str | None = None  # ПУБЛИЧНЫЙ адрес аккаунта, на котором торгует copy-бот (ключ не нужен)


class ApiCfg(_Model):
    base_url: str = "https://api.hyperliquid.xyz"
    ws_url: str = "wss://api.hyperliquid.xyz/ws"
    weight_budget_per_min: float = 1000
    max_concurrency: int = 4
    timeout_s: float = 30
    retries: int = 5
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60
    max_consecutive_failures: int = 8  # сетевых ошибок подряд → API недоступен, запуск останавливается
    ws_ping_s: float = 30.0  # сервер закрывает молчащее ~60 с соединение [api_notes §5]
    ws_pong_timeout_s: float = 10.0
    fills_page_max: int = 2000
    fills_available_max: int = 10_000
    range_page_max: int = 500
    candles_available_max: int = 5000
    candle_intervals: list[str] = ["1h", "15m", "1m"]


class StorageCfg(_Model):
    sqlite_path: str = "data/hl_scout.sqlite"
    reports_dir: str = "reports"


class LoggingCfg(_Model):
    level: str = "INFO"
    file: str | None = "logs/hl_scout.log"


class Config(_Model):
    deposit: DepositCfg = DepositCfg()
    goal: GoalCfg = GoalCfg()
    copying: CopyCfg = Field(default=CopyCfg(), alias="copy")  # YAML section `copy:`
    costs: CostsCfg = CostsCfg()
    universe: UniverseCfg = UniverseCfg()
    discovery: DiscoveryCfg = DiscoveryCfg()
    filters: FiltersCfg = FiltersCfg()
    score: ScoreCfg = ScoreCfg()
    recommend: RecommendCfg = RecommendCfg()
    backtest: BacktestCfg = BacktestCfg()
    montecarlo: MonteCarloCfg = MonteCarloCfg()
    rules: RulesCfg = RulesCfg()
    project: ProjectCfg = ProjectCfg()
    api: ApiCfg = ApiCfg()
    storage: StorageCfg = StorageCfg()
    logging: LoggingCfg = LoggingCfg()

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


# --- copy-bot semantics (copybot_fields.yaml) ---------------------------------


class CopySemantics(_Model):
    """How the third-party copy bot turns a trader action into my order. See copybot_fields.yaml."""

    event_granularity: Literal["order", "fill"] = "order"
    ratio_applies_to: Literal["order_size"] = "order_size"
    increase_without_position: Literal["open", "skip"] = "open"
    reduce_mode: Literal["ratio_of_order", "proportional"] = "ratio_of_order"
    full_close_on_trader_flat: bool = True
    small_size_applies_to_reduce: bool = True
    allow_small_full_close: bool = True
    margin_mode: Literal["cross", "isolated"] = "cross"
    price_sl_basis: Literal["price", "roe"] = "price"
    balance_sl_basis: Literal["level", "loss"] = "level"
    balance_trigger_action: Literal["close_all_and_stop"] = "close_all_and_stop"
    balance_tp_simulated: bool = False
    reenter_after_sl: bool = False
    buy_times_counts_open: bool = True
    follow_leverage_when_unknown: Literal["fixed"] = "fixed"


class CopyBotField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    unit: str
    logic: str
    status: str
    source: str | None = None


class CopyBotInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    docs_url: str | None = None
    fee_bps: float = 10.0
    fee_status: str = "unverified"


class CopyBotSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bot: CopyBotInfo = CopyBotInfo()
    semantics: CopySemantics = CopySemantics()
    fields: list[CopyBotField] = []

    @property
    def unverified_fields(self) -> list[str]:
        return [f.label for f in self.fields if f.status == "unverified"]

    @property
    def verified(self) -> bool:
        return not self.unverified_fields and self.bot.fee_status == "verified"


# --- secrets (.env) -------------------------------------------------------------


class Secrets(BaseSettings):
    """Secrets from environment / .env. Never logged."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None


# --- loading ----------------------------------------------------------------------


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидался YAML-словарь")
    return data


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config.yaml; missing keys fall back to the defaults above (which mirror the file)."""
    p = Path(path or os.environ.get("HL_SCOUT_CONFIG", "config.yaml"))
    if not p.exists():
        return Config()
    return Config.model_validate(_read_yaml(p))


def load_copybot(path: str | os.PathLike[str] | None = None) -> CopyBotSpec:
    p = Path(path or os.environ.get("HL_SCOUT_COPYBOT", "copybot_fields.yaml"))
    if not p.exists():
        return CopyBotSpec()
    return CopyBotSpec.model_validate(_read_yaml(p))
