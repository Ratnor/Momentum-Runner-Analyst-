from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scanner.config import DEFAULT_CONFIG_DIR


@pytest.fixture
def settings_dict() -> dict:
    """A fresh, editable copy of the real settings.yaml."""
    with (DEFAULT_CONFIG_DIR / "settings.yaml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def write_yaml(tmp_path: Path):
    def _write(data: dict, name: str = "settings.yaml") -> Path:
        p = tmp_path / name
        p.write_text(yaml.safe_dump(data), encoding="utf-8")
        return p

    return _write
