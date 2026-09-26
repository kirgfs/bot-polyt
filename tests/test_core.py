from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from polybot.core.config import (
    AppConfig,
    LiveTradingNotAllowedError,
    RecorderConfig,
    Settings,
    assert_live_trading_allowed,
)
from polybot.core.logging import mask_secrets, mask_text, short_id
from polybot.core.timeutil import NS_PER_S, parse_ts_ns


class TestParseTs:
    def test_gamma_game_start_format(self) -> None:
        # Real Gamma value (docs/api_notes.md §11, [CAP])
        assert parse_ts_ns("2026-08-18 01:15:00+00") == 1_787_015_700 * NS_PER_S

    def test_iso_z_with_fraction(self) -> None:
        assert parse_ts_ns("2026-08-18T02:26:06.831718Z") == 1_787_019_966_831_718_000

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(1_787_015_700, 1_787_015_700 * NS_PER_S), ("1787015700000", 1_787_015_700 * NS_PER_S)],
    )
    def test_epoch_units(self, value: object, expected: int) -> None:
        assert parse_ts_ns(value) == expected

    @pytest.mark.parametrize("value", [None, "", "  ", "soon", "2026-08-18 01:15:00", True, -5, {}])
    def test_rejects(self, value: object) -> None:
        # Naive datetimes are rejected: no guessing of time zones for start times.
        assert parse_ts_ns(value) is None

    @given(st.integers(min_value=10**9, max_value=4 * 10**9))
    def test_epoch_seconds_roundtrip(self, seconds: int) -> None:
        assert parse_ts_ns(seconds) == seconds * NS_PER_S
        assert parse_ts_ns(str(seconds * 1000)) == seconds * NS_PER_S


class TestMasking:
    def test_query_api_key(self) -> None:
        assert "SECRETVALUE" not in mask_text("GET /v4/odds?apiKey=SECRETVALUE&fixtureId=1")

    def test_private_key_hex(self) -> None:
        key = "0x" + "ab" * 32
        assert key not in mask_text(f"key is {key}")

    def test_uuid_api_key(self) -> None:
        assert "***" in mask_text("api 550e8400-e29b-41d4-a716-446655440000")

    def test_base64_secret(self) -> None:
        assert "***" in mask_text("secret Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MEFCQ0RFRkdISUpL=")

    def test_public_ids_stay_readable(self) -> None:
        token = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
        slug = "atp-lehecka-fils-2026-08-17-set-2-winner-Lehecka-vs-Fils"
        assert mask_text(f"{token} {slug}") == f"{token} {slug}"

    def test_short_id_is_not_masked(self) -> None:
        cid = "0x" + "12" * 32
        assert mask_text(short_id(cid)) == short_id(cid)

    def test_by_key_name(self) -> None:
        event = mask_secrets(None, "info", {"event": "x", "api_key": "abc", "token_id": "123"})
        assert event["api_key"] == "***"
        assert event["token_id"] == "123"


class TestConfig:
    def test_repo_config_is_valid(self, app_config: AppConfig) -> None:
        assert app_config.base.geoblock.allowed_countries == ("AM",)
        assert set(app_config.recorder.sports) == {"tennis", "soccer", "basketball"}
        assert app_config.recorder.oddspapi.mode == "off"

    def test_unknown_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecorderConfig.model_validate(
                {
                    "sports": {"tennis": {"tag_slugs": ["tennis"], "market_types": ["moneyline"]}},
                    "discovry": {},
                }
            )

    def test_secrets_not_in_repr(self) -> None:
        settings = Settings(_env_file=None, ODDSPAPI_API_KEY="very-secret-key")  # type: ignore[call-arg]
        assert "very-secret-key" not in repr(settings)
        assert settings.oddspapi_api_key is not None

    def test_empty_secret_is_none(self) -> None:
        settings = Settings(_env_file=None, CLOB_SECRET="")  # type: ignore[call-arg]
        assert settings.clob_secret is None


class TestLiveTradingGate:
    def test_paper_by_default(self) -> None:
        with pytest.raises(LiveTradingNotAllowedError):
            assert_live_trading_allowed(Settings(_env_file=None))  # type: ignore[call-arg]

    def test_live_mode_without_flag(self) -> None:
        settings = Settings(_env_file=None, MODE="live", LIVE_TRADING=False)  # type: ignore[call-arg]
        with pytest.raises(LiveTradingNotAllowedError):
            assert_live_trading_allowed(settings)

    def test_both_set(self) -> None:
        settings = Settings(_env_file=None, MODE="live", LIVE_TRADING=True)  # type: ignore[call-arg]
        assert_live_trading_allowed(settings)
