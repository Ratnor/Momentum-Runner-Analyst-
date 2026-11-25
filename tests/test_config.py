from __future__ import annotations

import dataclasses

import pytest

from scanner.config import ConfigError, load_keywords, load_settings


# ---- the real config files load and hold the spec's fixed values ---------- #


def test_real_settings_load_with_spec_values():
    s = load_settings()
    assert s.sleeve.starting_capital == 2500.0
    assert s.sleeve.kill_switch_equity == 1500.0
    assert s.sizing.risk_pct == 0.016
    assert s.sizing.stop_pct == 0.15
    assert s.sizing.max_position_dollars == 500.0
    assert s.sizing.max_open_positions == 5
    assert s.sizing.reentry_cooldown_sessions == 10
    assert s.sizing.trade_budget == 20
    assert s.scoring.body_weight == 0.4
    assert s.scoring.catalyst_max_age_days == 10
    assert s.sector.biotech_sic_codes == [2834, 2835, 2836, 8731]
    assert (s.filters.price_min, s.filters.price_max) == (1.0, 5.0)
    assert (s.filters.mcap_min, s.filters.mcap_max) == (5e6, 50e6)
    assert s.filters.float_max == 8e6
    assert s.filters.rvol_min == 3.0
    assert s.filters.dollar_vol_min == 500_000.0
    assert s.exits.default_strategy == "scale_trail"
    assert s.exits.target_multiple == 2.0
    assert s.exits.max_hold_sessions == 20
    assert s.broker.mode == "paper"
    assert s.broker.slippage_pct == 0.01


def test_risk_per_trade_matches_spec_example():
    s = load_settings()
    assert s.sizing.risk_pct * s.sleeve.starting_capital == pytest.approx(40.0)


def test_real_keywords_load_with_spec_weights():
    k = load_keywords()
    assert k.keywords["FDA approval"] == 10
    assert k.keywords["Phase 3"] == 6
    assert k.keywords["public offering"] == -8
    assert k.keywords["reverse merger"] == -5
    assert k.filing_triggers["ipo_low_float"].score == 6
    assert k.filing_triggers["refiled_s1"].score == 3
    assert k.filing_triggers["dilution_on_candidate"].score == -8
    assert "424B5" in k.dilution_exit_forms


def test_settings_are_read_only():
    s = load_settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.sizing.risk_pct = 0.05  # type: ignore[misc]


# ---- bad config is rejected with a useful message ------------------------- #


def test_missing_key_is_reported_with_path(settings_dict, write_yaml):
    del settings_dict["sizing"]["risk_pct"]
    with pytest.raises(ConfigError, match=r"settings\.sizing: missing key\(s\): risk_pct"):
        load_settings(write_yaml(settings_dict))


def test_unknown_key_is_reported(settings_dict, write_yaml):
    settings_dict["filters"]["price_mni"] = 1.0  # typo
    with pytest.raises(ConfigError, match="unknown key.*price_mni"):
        load_settings(write_yaml(settings_dict))


def test_wrong_type_is_reported(settings_dict, write_yaml):
    settings_dict["sizing"]["max_open_positions"] = "five"
    with pytest.raises(ConfigError, match=r"sizing\.max_open_positions: expected an integer"):
        load_settings(write_yaml(settings_dict))


def test_bool_is_not_accepted_as_number(settings_dict, write_yaml):
    settings_dict["sizing"]["risk_pct"] = True
    with pytest.raises(ConfigError, match="expected a number"):
        load_settings(write_yaml(settings_dict))


def test_int_is_accepted_for_float(settings_dict, write_yaml):
    settings_dict["sizing"]["max_position_dollars"] = 500
    s = load_settings(write_yaml(settings_dict))
    assert isinstance(s.sizing.max_position_dollars, float)


def test_unknown_exit_strategy_rejected(settings_dict, write_yaml):
    settings_dict["exits"]["default_strategy"] = "yolo"
    with pytest.raises(ConfigError, match="not one of"):
        load_settings(write_yaml(settings_dict))


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("filters", "price_min", 6.0, "price_min < price_max"),
        ("filters", "mcap_max", 1.0, "mcap_min < mcap_max"),
        ("filters", "allowed_exchanges", ["NASDAQ", "OTC Markets"], "OTC"),
        ("sleeve", "kill_switch_equity", 3000.0, "kill_switch_equity"),
        ("sizing", "risk_pct", 0.5, "risk_pct"),
        ("scoring", "body_weight", 1.5, "body_weight"),
        ("risk_flags", "dilution_penalty", 6, "dilution_penalty must be <= 0"),
        ("data", "sec_max_requests_per_sec", 10, "under SEC's limit"),
        ("backtest", "out_of_sample_start", "2024-06-01", "out-of-sample must start after"),
    ],
)
def test_range_checks(settings_dict, write_yaml, section, key, value, message):
    settings_dict[section][key] = value
    with pytest.raises(ConfigError, match=message):
        load_settings(write_yaml(settings_dict))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.yaml")


def test_empty_file(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="empty"):
        load_settings(p)


def test_duplicate_keywords_rejected(write_yaml):
    data = {
        "keywords": {"FDA approval": 10, "fda APPROVAL": 9, "public offering": -8},
        "filing_triggers": {},
        "dilution_exit_forms": [],
    }
    with pytest.raises(ConfigError, match="duplicate"):
        load_keywords(write_yaml(data, "keywords.yaml"))
