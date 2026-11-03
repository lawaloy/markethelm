"""Frozen, point-only ridge return candidate for prospective shadow evaluation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from importlib.resources import files
from typing import Mapping

MODEL_VERSION = "ridge-return-v1"
# Fixed before the archived diagnostic run; there is no parameter search.
RECIPE = {
    "version": "ridge-return-recipe-v1",
    "features": ["constant", "clipped incumbent return / 0.05"],
    "feature_return_scale": 0.05,
    "training_return_cap": 0.20,
    "prediction_return_cap": 0.05,
    "ridge_penalty": 1.0,
    "weighting": "equal origin-session weights, then equal stocks within session",
    "target": "exact five-session close / rounded projection origin price - 1",
    "outputs": "point only; no confidence or interval estimate",
}


@dataclass(frozen=True)
class ReturnCandidate:
    """All prediction math is fingerprinted with the class and frozen artifact."""

    artifact_json: str

    def __post_init__(self):
        artifact = json.loads(self.artifact_json)
        if (
            artifact["schema_version"] != 1
            or artifact["deployment"] != "shadow_only"
            or artifact["model_version"] != MODEL_VERSION
            or artifact["recipe"] != RECIPE
            or artifact["calendar"] != "XNYS"
            or artifact["horizon_sessions"] != 5
        ):
            raise ValueError("Unsupported return candidate artifact")
        coefficients = artifact["coefficients"]
        if len(coefficients) != 2 or any(
            type(x) not in (int, float) or not math.isfinite(x) for x in coefficients
        ):
            raise ValueError("Candidate coefficients must be two finite numbers")
        frozen = datetime.fromisoformat(artifact["frozen_at"])
        cutoff = datetime.fromisoformat(artifact["training"]["available_before"])
        if frozen.utcoffset() is None or cutoff.utcoffset() is None or cutoff >= frozen:
            raise ValueError("Candidate must be frozen after its training cutoff")

    @property
    def definition(self) -> dict:
        return json.loads(self.artifact_json)

    @property
    def frozen_at(self) -> datetime:
        return datetime.fromisoformat(self.definition["frozen_at"]).astimezone(timezone.utc)

    @staticmethod
    def feature(origin: float, incumbent: float) -> float:
        if not all(math.isfinite(value) and value > 0 for value in (origin, incumbent)):
            raise ValueError("Candidate requires finite positive prices")
        scale = RECIPE["feature_return_scale"]
        return max(-1.0, min(1.0, (incumbent / origin - 1) / scale))

    def target(self, origin: float, incumbent: float) -> float:
        feature = self.feature(origin, incumbent)
        intercept, slope = self.definition["coefficients"]
        cap = RECIPE["prediction_return_cap"]
        change = max(-cap, min(cap, intercept + slope * feature))
        target = origin * (1 + change)
        if not math.isfinite(target) or target <= 0:
            raise ValueError("Invalid candidate target")
        return target

    def predict(self, projection: Mapping, snapshot: Mapping) -> dict:
        if projection["projection_model_version"] != self.definition["incumbent_version"]:
            raise ValueError("Candidate requires the frozen incumbent feature version")
        generated = datetime.fromisoformat(str(projection["generated_at"]).replace("Z", "+00:00"))
        if generated.utcoffset() is None or generated <= self.frozen_at:
            raise ValueError("Candidate cannot predict before its artifact was frozen")
        return {
            "target_mid": self.target(
                float(projection["current_price"]), float(projection["target_mid"])
            )
        }


@lru_cache(maxsize=1)
def bundled_candidate() -> ReturnCandidate:
    """Use packaged coefficients only; live collection never trains a model."""
    artifact = files("src.analysis.models").joinpath("ridge_return_v1.json")
    return ReturnCandidate(artifact.read_text(encoding="utf-8"))
