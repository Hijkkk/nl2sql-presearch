"""
用于外部 NL2SQL 目录的轻量级加载器。
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from loguru import logger


DEFAULT_CATALOG_PATH = Path(r"Z:\MiniMax\nl2sql\datasources\nl2sql_data_v2\nl2sql_catalog.yaml")

_CACHE_PATH = ""
_CACHE_MTIME = 0.0
_CACHE_DATA: Dict[str, Any] = {}


def get_source_catalog(data_source: str) -> Dict[str, Any]:
    if not data_source:
        return {}
    sources = load_catalog().get("sources", {})
    catalog = sources.get(data_source, {}) or {}
    if catalog:
        return catalog
    aliases = {
        "mysql_police_address": "police_address",
    }
    return sources.get(aliases.get(data_source, ""), {}) or {}


def preferred_objects_for(data_source: str, available_names: Iterable[str]) -> List[str]:
    catalog = get_source_catalog(data_source)
    available_lookup = {str(name).lower(): str(name) for name in available_names}
    result: List[str] = []
    for item in catalog.get("preferred_objects", []) or []:
        actual = available_lookup.get(str(item).lower())
        if actual and actual not in result:
            result.append(actual)
    return result


def catalog_prompt_hint(data_source: str, available_names: Iterable[str]) -> str:
    catalog = get_source_catalog(data_source)
    if not catalog:
        return ""

    preferred = preferred_objects_for(data_source, available_names)
    lines = []
    if preferred:
        lines.append("Catalog preferred objects: " + ", ".join(preferred))
    default_time_field = catalog.get("default_time_field")
    if default_time_field:
        lines.append(f"Default time field: {default_time_field}")
    logical_foreign_keys = catalog.get("logical_foreign_keys") or []
    if logical_foreign_keys:
        lines.append("Logical foreign keys: " + "; ".join(map(str, logical_foreign_keys[:8])))
    synonyms = catalog.get("synonyms") or {}
    if synonyms:
        synonym_parts = []
        for key, values in list(synonyms.items())[:12]:
            if isinstance(values, list):
                synonym_parts.append(f"{key} -> {', '.join(map(str, values))}")
            else:
                synonym_parts.append(f"{key} -> {values}")
        lines.append("Business synonyms: " + "; ".join(synonym_parts))
    if not lines:
        return ""
    return "\n".join(lines)


def load_catalog() -> Dict[str, Any]:
    global _CACHE_PATH, _CACHE_MTIME, _CACHE_DATA

    path = Path(os.getenv("NL2SQL_CATALOG_PATH") or DEFAULT_CATALOG_PATH)
    try:
        stat = path.stat()
    except OSError:
        return {"sources": {}}

    cache_path = str(path)
    if _CACHE_DATA and _CACHE_PATH == cache_path and _CACHE_MTIME == stat.st_mtime:
        return _CACHE_DATA

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8-sig")

    data = _load_yaml(text)
    if not isinstance(data, dict):
        data = {"sources": {}}
    data.setdefault("sources", {})

    _CACHE_PATH = cache_path
    _CACHE_MTIME = stat.st_mtime
    _CACHE_DATA = data
    return data


def _load_yaml(text: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {"sources": {}}
    except Exception as exc:
        logger.debug(f"PyYAML unavailable or catalog parse failed, using fallback parser: {exc}")
        return _parse_catalog_subset(text)


def _parse_catalog_subset(text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"sources": {}}
    current_source: Dict[str, Any] | None = None
    current_source_name = ""
    nested_key = ""

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        source_match = re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)
        if source_match:
            current_source_name = source_match.group(1)
            current_source = {}
            result["sources"][current_source_name] = current_source
            nested_key = ""
            continue

        if current_source is None:
            continue

        key_value_match = re.match(r"^    ([A-Za-z0-9_]+):\s*(.*)$", line)
        if key_value_match:
            nested_key = ""
            key, value = key_value_match.group(1), key_value_match.group(2).strip()
            if value == "":
                current_source[key] = {} if key == "synonyms" else []
                nested_key = key
            else:
                current_source[key] = _parse_value(value)
            continue

        if nested_key == "synonyms":
            synonym_match = re.match(r"^      (.+?):\s*(.*)$", line)
            if synonym_match:
                current_source.setdefault("synonyms", {})[synonym_match.group(1).strip()] = _parse_value(
                    synonym_match.group(2).strip()
                )
            continue

        if nested_key == "logical_foreign_keys":
            item_match = re.match(r"^      -\s*(.*)$", line)
            if item_match:
                current_source.setdefault("logical_foreign_keys", []).append(item_match.group(1).strip())

    return result


def _parse_value(value: str) -> Any:
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        try:
            return ast.literal_eval(value)
        except Exception:
            body = value[1:-1].strip()
            if not body:
                return []
            return [item.strip().strip("\"'") for item in body.split(",")]
    return value.strip("\"'")
