"""Versioned pure scoring rule bound into each prospective protocol."""

import math

SCORING_VERSION = "exact-session-v1"


def score_prediction(prediction, origin_price: float, actual: float) -> dict:
    direction = int(actual > origin_price) - int(actual < origin_price)
    correct = int(direction == prediction["direction"])
    error = (
        None
        if prediction["target_mid"] is None
        else abs(prediction["target_mid"] - actual) / actual * 100
    )
    band = (
        None
        if prediction["target_low"] is None
        else int(prediction["target_low"] <= actual <= prediction["target_high"])
    )
    brier = (
        None
        if prediction["confidence"] is None
        else (prediction["confidence"] / 100 - correct) ** 2
    )
    result = {
        "abs_error_pct": error,
        "direction_correct": correct,
        "band_hit": band,
        "brier_score": brier,
    }
    if any(value is not None and not math.isfinite(value) for value in result.values()):
        raise ValueError("Score must be finite")
    return result
