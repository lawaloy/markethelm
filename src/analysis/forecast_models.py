"""Explicit model versions and a prospective protocol frozen before outcomes."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Mapping

from src.analysis.forecast_scoring import SCORING_VERSION, score_prediction
from src.analysis.projector import StockProjector

PROTOCOL_VERSION = "prospective-v1"
UNCHANGED_VERSION = "unchanged-price-v1"


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@lru_cache(maxsize=128)
def _source_fingerprint(function) -> str:
    return fingerprint(inspect.getsource(function).replace("\r\n", "\n"))


def protocol_definition() -> dict:
    return {
        "version": PROTOCOL_VERSION,
        "calendar": "XNYS",
        "horizon_sessions": 5,
        "scoring_rule_version": SCORING_VERSION,
        "scoring_implementation_sha256": _source_fingerprint(score_prediction),
        "confidence_cohorts": "ten-point score bins, with 90-100 inclusive",
        "comparison_weighting": "equal completed origin-session weights; "
        "error divided by actual close",
        "primary_input_rule": "same origin session quote at/after close minus 15 minutes "
        "and before generation",
        "incumbent_version": StockProjector.MODEL_VERSION,
        "price_reference": UNCHANGED_VERSION,
        "sampling": "first recorded case per symbol and completed origin session",
        "minimum_scored_cases": 200,
        "minimum_origin_sessions": 20,
        "minimum_symbols": 25,
        "minimum_paired_coverage_pct": 90,
        "minimum_relative_error_improvement_pct": 5,
        "minimum_session_win_fraction": 0.6,
        "confidence_reference_brier": 0.25,
        "minimum_confidence_coverage_pct": 90,
        "minimum_confidence_cohorts": 2,
        "minimum_cohort_samples": 30,
        "minimum_range_coverage_pct": 75,
        "range_nominal_coverage_pct": 80,
        "promotion": "manual review only; require price-reference and incumbent comparisons, "
        "confidence skill when supplied, and review coverage/width tradeoffs",
        "data": "only forecasts durably recorded before the exact target session close; "
        "only newly observed verified previous-close outcomes; no historical backfill",
    }


@dataclass(frozen=True)
class ForecastModel:
    version: str
    kind: str
    definition: dict
    predict: Callable[[Mapping, Mapping], dict]

    def manifest(self) -> dict:
        return {
            "version": self.version,
            "kind": self.kind,
            "definition": self.definition,
            "predictor_sha256": _source_fingerprint(self.predict),
        }


def _heuristic(projection: Mapping, snapshot: Mapping) -> dict:
    return {
        "target_mid": projection["target_mid"],
        "target_low": projection.get("target_low"),
        "target_high": projection.get("target_high"),
        "confidence": projection.get("confidence"),
    }


def _unchanged(projection: Mapping, snapshot: Mapping) -> dict:
    return {"target_mid": round(snapshot["close"], 2)}


def _up(projection: Mapping, snapshot: Mapping) -> dict:
    return {"direction": 1}


def _down(projection: Mapping, snapshot: Mapping) -> dict:
    return {"direction": -1}


def builtin_models() -> tuple[ForecastModel, ...]:
    return (
        ForecastModel(
            StockProjector.MODEL_VERSION,
            "point",
            {
                "method": "existing StockProjector",
                "implementation_sha256": _source_fingerprint(StockProjector),
                "horizon_sessions": 5,
                "calendar": "XNYS",
            },
            _heuristic,
        ),
        ForecastModel(
            UNCHANGED_VERSION,
            "point",
            {
                "method": "round origin close to cents and hold unchanged",
            },
            _unchanged,
        ),
        ForecastModel("always-up-v1", "direction", {"direction": 1}, _up),
        ForecastModel("always-down-v1", "direction", {"direction": -1}, _down),
    )
