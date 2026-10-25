"""Access to the authoritative OpenAPI contract (opal_talk.yaml)."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Any

import yaml

# The implemented contract is packaged as a real file (never a symlink, which breaks
# on Windows checkouts and in wheels). Selection is explicit: only this resource is
# loaded, so adding a newer contract file alongside it cannot change what is served.
CONTRACT_RESOURCE = "contract/opal_talk.yaml"


@lru_cache(maxsize=1)
def load_contract() -> dict[str, Any]:
    text = files("opal_talk").joinpath(CONTRACT_RESOURCE).read_text(encoding="utf-8")
    return yaml.safe_load(text)


def contract_operations() -> dict[str, tuple[str, str]]:
    """operationId -> (HTTP method upper-case, path)."""
    ops: dict[str, tuple[str, str]] = {}
    for path, item in load_contract()["paths"].items():
        for method, op in item.items():
            if method in {"get", "put", "post", "patch", "delete"}:
                ops[op["operationId"]] = (method.upper(), path)
    return ops
