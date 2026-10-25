"""Helpers that validate HTTP responses against opal_talk.yaml."""

from __future__ import annotations

import re
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from opal_talk.contract import load_contract

SPEC = load_contract()
METHODS = ("get", "put", "post", "patch", "delete")

# Transitional: service errors already emit the contract-1.2 `OpalError` envelope
# while the published contract is still v1.0, so error bodies are validated against
# this shape rather than the v1.0 error schemas they will replace.
OPAL_ERROR_SCHEMA = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["code", "message", "request_id", "transaction_id"],
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "request_id": {"type": "string"},
                "transaction_id": {"type": "string"},
            },
        }
    },
}


def _route_regex(template: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\{[^/]+\}", "[^/]+", template) + "$")


ROUTES = sorted(
    (
        (template.count("{"), _route_regex(template), method.upper(), op)
        for template, item in SPEC["paths"].items()
        for method, op in item.items()
        if method in METHODS
    ),
    key=lambda r: r[0],
)


def match_operation(method: str, path: str) -> dict[str, Any] | None:
    for _, regex, op_method, op in ROUTES:
        if op_method == method.upper() and regex.match(path):
            return op
    return None


def _deref(node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        target: Any = SPEC
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


def schema_validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator({**schema, "components": SPEC["components"]})


def validate_schema(instance: Any, schema_name: str) -> None:
    schema_validator({"$ref": f"#/components/schemas/{schema_name}"}).validate(instance)


def check_response(resp: httpx.Response, method: str | None = None, path: str | None = None) -> dict[str, Any] | None:
    """Assert the response status, headers and body conform to the matching contract operation."""
    method = method or resp.request.method
    path = path or resp.request.url.path
    op = match_operation(method, path)
    if op is None:
        return None
    status = str(resp.status_code)
    assert status in op["responses"], f"{op['operationId']} returned undocumented status {status}: {resp.text[:300]}"
    spec_resp = _deref(op["responses"][status])
    content = spec_resp.get("content")
    if not content:
        assert not resp.content, f"{op['operationId']} {status} must not have a body"
        return op
    media = resp.headers.get("content-type", "").split(";")[0].strip()
    assert media in content, f"{op['operationId']} {status} returned media type {media}, expected {list(content)}"
    if media == "application/json":
        if resp.status_code >= 400:
            Draft202012Validator(OPAL_ERROR_SCHEMA).validate(resp.json())
        else:
            schema_validator(content[media]["schema"]).validate(resp.json())
    required = _required_audio_headers(op, status)
    for name, header in (spec_resp.get("headers") or {}).items():
        if not name.startswith("X-Opal-"):
            continue
        header = _deref(header)
        if name in required:
            assert name.lower() in resp.headers, f"{op['operationId']} missing header {name}"
        if name.lower() not in resp.headers:
            continue
        value: Any = resp.headers[name]
        if _deref(header["schema"]).get("type") == "integer":
            value = int(value)
        schema_validator(header["schema"]).validate(value)
    return op


def _required_audio_headers(op: dict[str, Any], status: str) -> set[str]:
    if status != "200":
        return set()
    headers = _deref(op["responses"]["200"]).get("headers") or {}
    # X-Opal-Profile-Id is only present when a profile participated in resolution.
    return {h for h in headers if h.startswith("X-Opal-") and h != "X-Opal-Profile-Id"}
