"""Configuration shared by the hosted daily collector and its health probe."""

from __future__ import annotations

import os
from dataclasses import dataclass


def market_refresh_enabled() -> bool:
    return os.getenv("MARKETHELM_MARKET_REFRESH_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class MarketRefreshSettings:
    close_delay_seconds: int = 900
    timeout_seconds: int = 1800
    retry_seconds: int = 900
    max_attempts: int = 3
    min_symbols: int = 10
    top_n: int = 50
    poll_seconds: int = 60

    def __post_init__(self) -> None:
        bounds = {
            "close_delay_seconds": (0, 7200),
            "timeout_seconds": (1, 3600),
            "retry_seconds": (1, 86400),
            "max_attempts": (1, 10),
            "min_symbols": (1, 500),
            "top_n": (1, 500),
            "poll_seconds": (1, 3600),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"MARKET_REFRESH_{name.upper()} must be between {low} and {high}")
        if self.min_symbols > self.top_n:
            raise ValueError("MARKET_REFRESH_MIN_SYMBOLS must not exceed MARKET_REFRESH_TOP_N")

    @classmethod
    def from_env(cls) -> MarketRefreshSettings:
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            key = "MARKET_REFRESH_" + name.upper()
            try:
                values[name] = int(os.getenv(key, str(getattr(defaults, name))))
            except ValueError as exc:
                raise ValueError(f"{key} must be an integer") from exc
        return cls(**values)
