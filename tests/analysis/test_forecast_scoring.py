"""Exact-session scoring must stay finite and omit fields the model did not produce."""

import math

import pytest

from src.analysis.forecast_scoring import score_prediction


def test_point_forecast_scores_error_band_and_brier():
    result = score_prediction(
        {
            "direction": 1,
            "target_mid": 102,
            "target_low": 99,
            "target_high": 105,
            "confidence": 80,
        },
        100,
        104,
    )
    assert result["abs_error_pct"] == pytest.approx(2 / 104 * 100)
    assert result["direction_correct"] == 1
    assert result["band_hit"] == 1
    assert result["brier_score"] == pytest.approx(0.04)


def test_direction_only_and_unchanged_actual_omit_price_error():
    up = score_prediction(
        {
            "direction": 1,
            "target_mid": None,
            "target_low": None,
            "target_high": None,
            "confidence": None,
        },
        100,
        100,
    )
    unchanged = score_prediction(
        {
            "direction": 0,
            "target_mid": 100,
            "target_low": None,
            "target_high": None,
            "confidence": None,
        },
        100,
        100,
    )
    assert up == {
        "abs_error_pct": None,
        "direction_correct": 0,
        "band_hit": None,
        "brier_score": None,
    }
    assert unchanged["direction_correct"] == 1
    assert unchanged["abs_error_pct"] == 0.0
    assert unchanged["band_hit"] is None


def test_missed_band_and_wrong_direction_are_explicit_zeros():
    result = score_prediction(
        {
            "direction": 1,
            "target_mid": 102,
            "target_low": 99,
            "target_high": 105,
            "confidence": 80,
        },
        100,
        90,
    )
    assert result["direction_correct"] == 0
    assert result["band_hit"] == 0
    assert result["abs_error_pct"] == pytest.approx(12 / 90 * 100)
    assert result["brier_score"] == pytest.approx(0.64)


@pytest.mark.parametrize("mid", [float("inf"), float("nan")])
def test_nonfinite_scores_are_rejected(mid):
    with pytest.raises(ValueError, match="finite"):
        score_prediction(
            {
                "direction": 1,
                "target_mid": mid,
                "target_low": None,
                "target_high": None,
                "confidence": None,
            },
            100,
            104,
        )
    assert not math.isfinite(mid)
