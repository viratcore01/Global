"""YAML loading that works with either PyYAML or ruamel.yaml."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from .util import get_logger

log = get_logger(__name__)


def load_yaml_text(text: str) -> Any:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover - malformed YAML
        raise ValueError(f"Invalid YAML: {exc}") from exc

    try:
        from ruamel.yaml import YAML

        y = YAML(typ="safe")
        return y.load(io.StringIO(text))
    except ImportError as exc:
        raise RuntimeError(
            "No YAML library found. Install ruamel.yaml (recommended) or PyYAML."
        ) from exc
    except Exception as exc:  # pragma: no cover - malformed YAML
        raise ValueError(f"Invalid YAML: {exc}") from exc


def load_yaml(path: Path | str, *, default: Any = None, required: bool = False) -> Any:
    path = Path(path)
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Required YAML file not found: {path}")
        return default
    text = path.read_text(encoding="utf-8", errors="replace")
    data = load_yaml_text(text)
    if data is None:
        return default
    return data


def dump_yaml_text(data: Any) -> str:
    try:
        from ruamel.yaml import YAML

        y = YAML()
        y.default_flow_style = False
        stream = io.StringIO()
        y.dump(data, stream)
        return stream.getvalue()
    except ImportError:
        pass
    try:
        import yaml  # type: ignore

        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    except ImportError as exc:
        raise RuntimeError(
            "No YAML library found. Install ruamel.yaml (recommended) or PyYAML."
        ) from exc


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base (base is not mutated)."""
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
