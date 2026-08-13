"""Shared `Endpoint` record every framework extractor returns (SPEC.md §6
Phase 2 task 4)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    method: str | None  # "GET", "POST", ... — None for a route that handles any method
    route: str
    framework: str
    handler_symbol: str | None
    line: int
    auth_hint: str | None
    params_json: str | None
    source: str  # "ast" | "openapi" | "config"


def strip_py_string(raw: str) -> str:
    for quote in ('"""', "'''", '"', "'"):
        if raw.startswith(quote) and raw.endswith(quote) and len(raw) >= 2 * len(quote):
            return raw[len(quote) : -len(quote)]
    return raw
