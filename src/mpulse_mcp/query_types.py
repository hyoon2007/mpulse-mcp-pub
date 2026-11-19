"""Typed access to API metadata stored only in the packaged catalog.json.

Keep the public constants, QueryTypeMeta, is_known(), and describe() interfaces
stable. There is no second Python endpoint table or fallback copy of API facts.
Catalog changes take effect after process restart. Missing/invalid required
metadata is a configuration error rather than silently weakened validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .catalog import load
from .errors import ConfigError


def _invalid(field: str) -> ConfigError:
    return ConfigError(
        f"Invalid or missing API metadata in catalog.json: {field}.",
        hint="Restore the packaged catalog.json, validate it with the catalog tests, and restart the MCP server.",
    )


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(field)
    return value


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if (not isinstance(value, list) or not value
            or any(not isinstance(v, str) or not v for v in value)
            or len(set(value)) != len(value)):
        raise _invalid(field)
    return tuple(value)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise _invalid(field)
    return value


@dataclass(frozen=True)
class QueryTypeMeta:
    slug: str
    summary: str
    value_params: tuple[str, ...] = ()
    supports_time: bool = True
    supports_drilldown: bool = True
    notes: str = ""
    response_shape: str = ""
    aggregation: str = ""
    special_params: tuple[str, ...] = ()

    def all_params(self) -> list[str]:
        params = [*self.value_params, *self.special_params]
        if self.supports_time:
            params.extend(TIME_PARAMS)
        if self.supports_drilldown:
            params.extend(DRILLDOWN_PARAMS)
            params.extend(DYNAMIC_DRILLDOWN_PARAMS)
        params.append(FORMAT_PARAM)
        return list(dict.fromkeys(params))


def _load_metadata(data: dict[str, Any]) -> tuple[
    dict[str, QueryTypeMeta], tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...], str, str
]:
    """Validate reference data and build typed views; no API-specific literals."""
    common = _object(data.get("common_params"), "common_params")
    time = _object(common.get("time_selection"), "common_params.time_selection")
    time_params = _strings(time.get("parameters"), "common_params.time_selection.parameters")
    format_spec = _object(common.get("format"), "common_params.format")
    format_param = _text(format_spec.get("param_name"), "common_params.format.param_name")
    if not format_param:
        raise _invalid("common_params.format.param_name")
    comparator = _object(common.get("date_comparator"), "common_params.date_comparator")
    comparators = _strings(comparator.get("docs_enum"), "common_params.date_comparator.docs_enum")
    dims = _object(data.get("dimensions"), "dimensions")
    filters = _object(dims.get("drilldown_filter_params"), "dimensions.drilldown_filter_params")
    filter_names = _strings(filters.get("params"), "dimensions.drilldown_filter_params.params")
    drilldowns = tuple(k for k in filter_names if "{" not in k)
    dynamic = tuple(k for k in filter_names if "{" in k)
    usage = _object(data.get("query_types"), "query_types")
    constraints = _text(usage.get("usage_constraints"), "query_types.usage_constraints")
    endpoints = _object(data.get("endpoint_params"), "endpoint_params")
    types: dict[str, QueryTypeMeta] = {}
    for slug, raw in endpoints.items():
        if slug.startswith("_"):
            continue
        ep = _object(raw, f"endpoint_params.{slug}")
        if not isinstance(ep.get("supports_time"), bool) or not isinstance(ep.get("filters"), bool):
            raise _invalid(f"endpoint_params.{slug}.supports_time/filters")
        values = _object(ep.get("value_params"), f"endpoint_params.{slug}.value_params")
        specials = _object(ep.get("special_params"), f"endpoint_params.{slug}.special_params")
        types[slug] = QueryTypeMeta(
            slug=slug,
            summary=_text(ep.get("summary"), f"endpoint_params.{slug}.summary"),
            value_params=tuple(values),
            special_params=tuple(specials),
            supports_time=ep["supports_time"],
            supports_drilldown=ep["filters"],
            notes=_text(ep.get("notes"), f"endpoint_params.{slug}.notes"),
            response_shape=_text(ep.get("response_shape"), f"endpoint_params.{slug}.response_shape"),
            aggregation=_text(ep.get("aggregation"), f"endpoint_params.{slug}.aggregation"),
        )
    if not types:
        raise _invalid("endpoint_params (no query types)")
    return types, time_params, drilldowns, dynamic, comparators, constraints, format_param


(QUERY_TYPES, TIME_PARAMS, DRILLDOWN_PARAMS, DYNAMIC_DRILLDOWN_PARAMS,
 KNOWN_DATE_COMPARATORS, QUERY_CONSTRAINTS, FORMAT_PARAM) = _load_metadata(load())


def is_known(slug: str) -> bool:
    return slug in QUERY_TYPES


def describe(slug: str) -> QueryTypeMeta | None:
    return QUERY_TYPES.get(slug)
