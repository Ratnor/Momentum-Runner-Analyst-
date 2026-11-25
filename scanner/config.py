"""Load and validate config/settings.yaml and config/keywords.yaml.

Every threshold the program uses lives in those two files. This module turns
them into frozen (read-only) dataclasses and fails loudly at startup if a key
is missing, misspelled, the wrong type, or out of range, so a bad config
never reaches a trading decision.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


class ConfigError(ValueError):
    """Raised when a config file is missing keys, has unknown keys, or bad values."""


# --------------------------------------------------------------------------- #
# settings.yaml
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SleeveConfig:
    starting_capital: float
    kill_switch_equity: float
    kill_switch_reset: bool


@dataclass(frozen=True)
class SizingConfig:
    risk_pct: float
    stop_pct: float
    stop_below_low_offset: float
    max_position_dollars: float
    max_open_positions: int
    reentry_cooldown_sessions: int
    trade_budget: int


@dataclass(frozen=True)
class ScoringConfig:
    headline_min_score: float
    body_weight: float
    catalyst_max_age_days: int


@dataclass(frozen=True)
class SectorConfig:
    biotech_sic_codes: list[int]
    prerev_max_revenue: float


@dataclass(frozen=True)
class FiltersConfig:
    allowed_exchanges: list[str]
    price_min: float
    price_max: float
    mcap_min: float
    mcap_max: float
    float_max: float
    rvol_min: float
    rvol_lookback_days: int
    dollar_vol_min: float
    spread_proxy_max: float
    spread_max: float


@dataclass(frozen=True)
class RiskFlagsConfig:
    dilution_lookback_days: int
    dilution_penalty: float
    lockup_window_days: int
    lockup_penalty: float
    holder_13d_min_pct: float
    holder_13d_penalty: float
    insider_selling_lookback_days: int
    insider_selling_penalty: float
    short_interest_min_pct: float
    short_interest_bonus: float
    reverse_split_lookback_days: int
    reverse_split_penalty: float


@dataclass(frozen=True)
class EntryConfig:
    pullback_window_days: int
    fill_window_days: int
    buy_stop_offset: float
    limit_above_trigger_pct: float


ExitStrategyName = Literal["fixed_double", "scale_trail", "trail_only"]


@dataclass(frozen=True)
class ExitsConfig:
    default_strategy: ExitStrategyName
    target_multiple: float
    target_sell_fraction: float
    trail_pct: float
    time_stop_sessions: int
    time_stop_min_gain: float
    max_hold_sessions: int


@dataclass(frozen=True)
class BrokerConfig:
    mode: Literal["paper", "manual", "ibkr"]
    slippage_pct: float
    commission_per_trade: float


@dataclass(frozen=True)
class DataConfig:
    db_path: str
    cache_dir: str
    reports_dir: str
    sec_max_requests_per_sec: float
    http_timeout_sec: float
    http_max_retries: int


@dataclass(frozen=True)
class WireFeed:
    name: str
    url: str


@dataclass(frozen=True)
class SourcesConfig:
    sec_user_agent_env: str
    generic_user_agent: str
    ticker_map_url: str
    submissions_url: str
    edgar_current_url: str
    edgar_page_size: int
    edgar_max_pages: int
    edgar_forms: list[str]
    edgar_release_items: list[str]
    edgar_max_release_fetches: int
    max_tickers_per_item: int
    wire_feeds: list[WireFeed]
    yahoo_headline_url: str


@dataclass(frozen=True)
class ScheduleConfig:
    timezone: str
    market_calendar: str


@dataclass(frozen=True)
class AlertsConfig:
    primary: Literal["telegram", "gmail"]
    fallback: Literal["telegram", "gmail", "none"]


@dataclass(frozen=True)
class BacktestConfig:
    in_sample_start: str
    in_sample_end: str
    out_of_sample_start: str
    out_of_sample_end: str
    after_close_cutoff: str
    mcap_buckets: list[float]


@dataclass(frozen=True)
class Settings:
    sleeve: SleeveConfig
    sizing: SizingConfig
    scoring: ScoringConfig
    sector: SectorConfig
    filters: FiltersConfig
    risk_flags: RiskFlagsConfig
    entry: EntryConfig
    exits: ExitsConfig
    broker: BrokerConfig
    data: DataConfig
    sources: SourcesConfig
    schedule: ScheduleConfig
    alerts: AlertsConfig
    backtest: BacktestConfig


# --------------------------------------------------------------------------- #
# keywords.yaml
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FilingTrigger:
    score: float
    forms: list[str]


@dataclass(frozen=True)
class FilingRules:
    registration_forms: list[str]
    withdrawal_forms: list[str]
    periodic_report_forms: list[str]
    ipo_lookback_days: int


@dataclass(frozen=True)
class KeywordConfig:
    keywords: dict[str, float]
    filing_triggers: dict[str, FilingTrigger]
    filing_rules: FilingRules
    dilution_exit_forms: list[str]


# --------------------------------------------------------------------------- #
# Generic builder: YAML dict -> typed dataclass, with path-aware errors
# --------------------------------------------------------------------------- #


def _build(tp: Any, value: Any, path: str) -> Any:
    """Convert a parsed-YAML value into type ``tp``; raise ConfigError on mismatch."""
    origin = typing.get_origin(tp)

    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected a mapping, got {type(value).__name__}")
        hints = typing.get_type_hints(tp)
        names = [f.name for f in dataclasses.fields(tp)]
        missing = [n for n in names if n not in value]
        unknown = [k for k in value if k not in names]
        if missing:
            raise ConfigError(f"{path}: missing key(s): {', '.join(missing)}")
        if unknown:
            raise ConfigError(f"{path}: unknown key(s): {', '.join(map(str, unknown))}")
        return tp(**{n: _build(hints[n], value[n], f"{path}.{n}") for n in names})

    if origin is Literal:
        allowed = typing.get_args(tp)
        if value not in allowed:
            raise ConfigError(f"{path}: {value!r} is not one of {list(allowed)}")
        return value

    if origin is list:
        (item_tp,) = typing.get_args(tp)
        if not isinstance(value, list):
            raise ConfigError(f"{path}: expected a list, got {type(value).__name__}")
        return [_build(item_tp, v, f"{path}[{i}]") for i, v in enumerate(value)]

    if origin is dict:
        key_tp, val_tp = typing.get_args(tp)
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected a mapping, got {type(value).__name__}")
        return {
            _build(key_tp, k, f"{path} key"): _build(val_tp, v, f"{path}[{k!r}]")
            for k, v in value.items()
        }

    if origin in (typing.Union, types.UnionType):
        raise ConfigError(f"{path}: union types are not supported in config")

    # Scalars. bool is a subclass of int in Python, so reject it explicitly.
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{path}: expected true/false, got {value!r}")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path}: expected an integer, got {value!r}")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{path}: expected a number, got {value!r}")
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(f"{path}: expected a string, got {value!r}")
        return value

    raise ConfigError(f"{path}: unsupported config type {tp!r}")  # pragma: no cover


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        raise ConfigError(f"config file is empty: {path}")
    return data


# --------------------------------------------------------------------------- #
# Range / consistency checks (things a type check can't catch)
# --------------------------------------------------------------------------- #


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _validate_settings(s: Settings) -> None:
    _check(s.sleeve.starting_capital > 0, "sleeve.starting_capital must be > 0")
    _check(
        0 < s.sleeve.kill_switch_equity < s.sleeve.starting_capital,
        "sleeve.kill_switch_equity must be between 0 and starting_capital",
    )
    _check(0 < s.sizing.risk_pct < 0.10, "sizing.risk_pct must be in (0, 0.10)")
    _check(0 < s.sizing.stop_pct < 1, "sizing.stop_pct must be in (0, 1)")
    _check(s.sizing.stop_below_low_offset >= 0, "sizing.stop_below_low_offset must be >= 0")
    _check(s.sizing.max_position_dollars > 0, "sizing.max_position_dollars must be > 0")
    _check(s.sizing.max_open_positions >= 1, "sizing.max_open_positions must be >= 1")
    _check(s.sizing.reentry_cooldown_sessions >= 0, "sizing.reentry_cooldown_sessions must be >= 0")
    _check(s.sizing.trade_budget >= 1, "sizing.trade_budget must be >= 1")

    _check(0 <= s.scoring.body_weight <= 1, "scoring.body_weight must be in [0, 1]")
    _check(s.scoring.catalyst_max_age_days >= 0, "scoring.catalyst_max_age_days must be >= 0")

    _check(len(s.sector.biotech_sic_codes) > 0, "sector.biotech_sic_codes must not be empty")
    _check(s.sector.prerev_max_revenue >= 0, "sector.prerev_max_revenue must be >= 0")

    f = s.filters
    _check(len(f.allowed_exchanges) > 0, "filters.allowed_exchanges must not be empty")
    _check(
        not any("OTC" in e.upper() for e in f.allowed_exchanges),
        "filters.allowed_exchanges must not include OTC markets",
    )
    _check(0 < f.price_min < f.price_max, "filters: need 0 < price_min < price_max")
    _check(0 < f.mcap_min < f.mcap_max, "filters: need 0 < mcap_min < mcap_max")
    _check(f.float_max > 0, "filters.float_max must be > 0")
    _check(f.rvol_min > 0, "filters.rvol_min must be > 0")
    _check(f.rvol_lookback_days >= 1, "filters.rvol_lookback_days must be >= 1")
    _check(f.dollar_vol_min >= 0, "filters.dollar_vol_min must be >= 0")
    _check(f.spread_proxy_max > 0, "filters.spread_proxy_max must be > 0")
    _check(0 < f.spread_max < 1, "filters.spread_max must be in (0, 1)")

    r = s.risk_flags
    for name in ("dilution", "lockup", "holder_13d", "insider_selling", "reverse_split"):
        _check(getattr(r, f"{name}_penalty") <= 0, f"risk_flags.{name}_penalty must be <= 0")
    _check(r.short_interest_bonus >= 0, "risk_flags.short_interest_bonus must be >= 0")
    _check(0 < r.holder_13d_min_pct < 1, "risk_flags.holder_13d_min_pct must be in (0, 1)")
    _check(0 < r.short_interest_min_pct < 1, "risk_flags.short_interest_min_pct must be in (0, 1)")

    e = s.entry
    _check(e.pullback_window_days >= 1, "entry.pullback_window_days must be >= 1")
    _check(
        e.fill_window_days >= e.pullback_window_days,
        "entry.fill_window_days must be >= entry.pullback_window_days",
    )
    _check(e.limit_above_trigger_pct >= 0, "entry.limit_above_trigger_pct must be >= 0")

    x = s.exits
    _check(x.target_multiple > 1, "exits.target_multiple must be > 1")
    _check(0 < x.target_sell_fraction <= 1, "exits.target_sell_fraction must be in (0, 1]")
    _check(0 < x.trail_pct < 1, "exits.trail_pct must be in (0, 1)")
    _check(x.time_stop_sessions >= 1, "exits.time_stop_sessions must be >= 1")
    _check(x.max_hold_sessions >= x.time_stop_sessions, "exits.max_hold_sessions must be >= time_stop_sessions")

    _check(0 <= s.broker.slippage_pct < 0.10, "broker.slippage_pct must be in [0, 0.10)")
    _check(s.broker.commission_per_trade >= 0, "broker.commission_per_trade must be >= 0")

    _check(
        0 < s.data.sec_max_requests_per_sec < 10,
        "data.sec_max_requests_per_sec must stay under SEC's limit of 10",
    )
    _check(s.data.http_max_retries >= 0, "data.http_max_retries must be >= 0")

    src = s.sources
    _check(1 <= src.edgar_page_size <= 100, "sources.edgar_page_size must be in [1, 100]")
    _check(src.edgar_max_pages >= 1, "sources.edgar_max_pages must be >= 1")
    _check(src.edgar_max_release_fetches >= 0, "sources.edgar_max_release_fetches must be >= 0")
    _check(src.max_tickers_per_item >= 1, "sources.max_tickers_per_item must be >= 1")
    names = [f.name for f in src.wire_feeds]
    _check(len(names) == len(set(names)), "sources.wire_feeds names must be unique")
    for tmpl, field in ((src.submissions_url, "{cik"), (src.yahoo_headline_url, "{ticker}")):
        _check(field in tmpl, f"sources: URL template {tmpl!r} must contain {field}")
    for field in ("{form}", "{start}", "{count}"):
        _check(field in src.edgar_current_url, f"sources.edgar_current_url must contain {field}")

    _check(s.alerts.primary != s.alerts.fallback, "alerts.fallback must differ from alerts.primary")

    b = s.backtest
    _check(b.in_sample_start < b.in_sample_end, "backtest: in-sample start must precede end")
    _check(b.in_sample_end < b.out_of_sample_start, "backtest: out-of-sample must start after in-sample ends")
    _check(b.out_of_sample_start < b.out_of_sample_end, "backtest: out-of-sample start must precede end")
    _check(b.mcap_buckets == sorted(b.mcap_buckets), "backtest.mcap_buckets must be ascending")


def _validate_keywords(k: KeywordConfig) -> None:
    _check(len(k.keywords) > 0, "keywords: table must not be empty")
    lowered = [kw.strip().lower() for kw in k.keywords]
    _check(all(lowered), "keywords: phrases must not be blank")
    dupes = sorted({kw for kw in lowered if lowered.count(kw) > 1})
    _check(not dupes, f"keywords: duplicate phrases (case-insensitive): {dupes}")
    _check(any(w < 0 for w in k.keywords.values()), "keywords: need at least one negative keyword")
    required = {"ipo_low_float", "refiled_s1", "dilution_on_candidate"}
    missing = required - set(k.filing_triggers)
    _check(not missing, f"filing_triggers: missing {sorted(missing)}")
    _check(k.filing_rules.ipo_lookback_days >= 1, "filing_rules.ipo_lookback_days must be >= 1")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def load_settings(path: str | Path | None = None) -> Settings:
    """Load and validate settings.yaml (defaults to config/settings.yaml)."""
    p = Path(path) if path else DEFAULT_CONFIG_DIR / "settings.yaml"
    settings = _build(Settings, _read_yaml(p), "settings")
    _validate_settings(settings)
    return settings


def load_keywords(path: str | Path | None = None) -> KeywordConfig:
    """Load and validate keywords.yaml (defaults to config/keywords.yaml)."""
    p = Path(path) if path else DEFAULT_CONFIG_DIR / "keywords.yaml"
    kw = _build(KeywordConfig, _read_yaml(p), "keywords")
    _validate_keywords(kw)
    return kw
