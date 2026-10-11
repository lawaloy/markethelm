"""Tests for the manual-publish dev version helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "dev_version.py"
SPEC = importlib.util.spec_from_file_location("dev_version", MODULE_PATH)
assert SPEC is not None
dev_version = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dev_version)


def test_compute_uses_next_patch_and_run_number() -> None:
    assert dev_version.compute_dev_version("0.3.7", 42) == "0.3.8.dev42"
    assert dev_version.compute_dev_version("1.9.99", 1) == "1.9.100.dev1"


def test_dev_version_sorts_between_base_and_next_release() -> None:
    packaging_version = pytest.importorskip("packaging.version")
    dev = packaging_version.Version(dev_version.compute_dev_version("0.3.7", 5))
    assert packaging_version.Version("0.3.7") < dev < packaging_version.Version("0.3.8")


def test_unique_per_run_number() -> None:
    assert dev_version.compute_dev_version("0.3.7", 5) != dev_version.compute_dev_version(
        "0.3.7", 6
    )


@pytest.mark.parametrize("bad", ["0.3", "v0.3.7", "0.3.7.dev1", ""])
def test_rejects_non_semver_base(bad: str) -> None:
    with pytest.raises(ValueError):
        dev_version.compute_dev_version(bad, 1)


def test_rejects_bad_run_number() -> None:
    with pytest.raises(ValueError):
        dev_version.compute_dev_version("0.3.7", 0)


def test_apply_rewrites_setup_cfg(tmp_path: Path) -> None:
    cfg = tmp_path / "setup.cfg"
    cfg.write_text("[metadata]\nname = markethelm\nversion = 0.3.7\n", encoding="utf-8")
    assert dev_version.apply_dev_version(cfg, 9) == "0.3.8.dev9"
    assert "version = 0.3.8.dev9" in cfg.read_text(encoding="utf-8")
