"""Access to the authoritative OpenAPI contract (opal_talk.yaml)."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Any

import yaml


@lru_cache(maxsize=1)
def load_contract() -> dict[str, Any]:
    text = files("opal_talk").joinpath("contract/opal_talk.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def contract_operations() -> dict[str, tuple[str, str]]:
    """operationId -> (HTTP method upper-case, path)."""
    ops: dict[str, tuple[str, str]] = {}
    for path, item in load_contract()["paths"].items():
        for method, op in item.items():
            if method in {"get", "put", "post", "patch", "delete"}:
                ops[op["operationId"]] = (method.upper(), path)
    return ops
