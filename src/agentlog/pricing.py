from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_PRICING_PATH = Path(__file__).resolve().with_name("pricing.toml")


@dataclass(frozen=True)
class ModelRates:
    model_id: str
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    cached_input_per_mtok: float | None = None

    def has_any_rate(self) -> bool:
        return any(
            v is not None
            for v in (
                self.input_per_mtok,
                self.output_per_mtok,
                self.cache_read_per_mtok,
                self.cache_write_per_mtok,
                self.cached_input_per_mtok,
            )
        )


@dataclass(frozen=True)
class PricingTable:
    version: str
    as_of: str
    models: dict[str, ModelRates]

    def rates_for(self, model: str | None) -> ModelRates | None:
        if not model:
            return None
        rates = self.models.get(model)
        if rates is None or not rates.has_any_rate():
            return None
        return rates


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_pricing(path: Path | None = None) -> PricingTable:
    p = path if path is not None else DEFAULT_PRICING_PATH
    if not p.is_file():
        return PricingTable(version="missing", as_of="", models={})
    with p.open("rb") as f:
        data = tomllib.load(f)
    models: dict[str, ModelRates] = {}
    for row in data.get("models") or []:
        if not isinstance(row, dict):
            continue
        mid = row.get("id")
        if not isinstance(mid, str) or not mid.strip():
            continue
        rates = ModelRates(
            model_id=mid.strip(),
            input_per_mtok=_float_or_none(row.get("input_per_mtok")),
            output_per_mtok=_float_or_none(row.get("output_per_mtok")),
            cache_read_per_mtok=_float_or_none(row.get("cache_read_per_mtok")),
            cache_write_per_mtok=_float_or_none(row.get("cache_write_per_mtok")),
            cached_input_per_mtok=_float_or_none(row.get("cached_input_per_mtok")),
        )
        models[rates.model_id] = rates
    return PricingTable(
        version=str(data.get("version") or "0"),
        as_of=str(data.get("as_of") or ""),
        models=models,
    )


@lru_cache(maxsize=4)
def _cached_pricing(path_str: str) -> PricingTable:
    return load_pricing(Path(path_str))


def get_pricing(path: Path | None = None) -> PricingTable:
    p = path if path is not None else DEFAULT_PRICING_PATH
    return _cached_pricing(str(p))


def _component(tokens: int | None, rate: float | None) -> float | None:
    if tokens is None or rate is None:
        return None
    return (tokens / 1_000_000.0) * rate


def estimate_cost(
    *,
    model: str | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    cache_write_input_tokens: int | None = None,
    pricing: PricingTable | None = None,
) -> dict[str, Any]:
    """Estimate USD cost. Unconfigured models stay status=unavailable."""
    table = pricing if pricing is not None else get_pricing()
    rates = table.rates_for(model)
    if rates is None:
        return {
            "status": "unavailable",
            "model": model,
            "pricing_table_version": table.version,
            "as_of": table.as_of,
            "message": (
                "No rates configured for this model in pricing.toml; "
                "cost is not estimated from invented numbers."
            ),
            "usd": None,
        }

    parts = {
        "input": _component(input_tokens, rates.input_per_mtok),
        "output": _component(output_tokens, rates.output_per_mtok),
        "cache_read": _component(
            cache_read_input_tokens, rates.cache_read_per_mtok
        ),
        "cache_write": _component(
            cache_creation_input_tokens
            if cache_creation_input_tokens is not None
            else cache_write_input_tokens,
            rates.cache_write_per_mtok,
        ),
        "cached_input": _component(
            cached_input_tokens, rates.cached_input_per_mtok
        ),
    }
    known = [v for v in parts.values() if v is not None]
    if not known:
        return {
            "status": "unavailable",
            "model": model,
            "pricing_table_version": table.version,
            "as_of": table.as_of,
            "message": "Model has a pricing row but no usable token×rate pairs.",
            "usd": None,
            "components": parts,
        }
    return {
        "status": "estimated",
        "model": model,
        "pricing_table_version": table.version,
        "as_of": table.as_of,
        "usd": sum(known),
        "components": parts,
    }
