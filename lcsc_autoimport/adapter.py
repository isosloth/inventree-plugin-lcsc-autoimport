import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _deep_get(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _normalize_field_name(name: Any) -> str:
    text = _clean_text(name)
    if not text:
        return ""
    text = text.replace("_", " ").replace("-", " ")
    while "  " in text:
        text = text.replace("  ", " ")
    return text.title()


def _normalize_parameter_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return str(value)
    return _clean_text(value)


def normalize_lcsc_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize multiple LCSC-compatible response shapes into a common dictionary."""
    if not isinstance(payload, dict):
        raise ValueError("LCSC payload must be a dictionary object")

    root = payload

    # Common wrapper shapes: {data: {...}}, {product: {...}}, {result: {...}}
    if not root.get("productCode") and not root.get("sku"):
        for key in ("data", "product", "result", "response"):
            nested = root.get(key)
            if isinstance(nested, dict):
                root = nested
                break

    sku = _first_present(
        root.get("productCode"),
        root.get("sku"),
        root.get("code"),
        root.get("lcsc")
    )
    if sku is None:
        raise ValueError("LCSC payload missing product code / SKU")

    category_name = _first_present(
        root.get("categoryName"),
        root.get("catName"),
        root.get("category"),
        root.get("type"),
        "Uncategorized",
    )

    name = _first_present(
        root.get("productName"),
        root.get("name"),
        root.get("productModel"),
        root.get("model"),
        str(sku),
    )

    description = _first_present(
        root.get("description"),
        root.get("productIntroEn"),
        root.get("productIntro"),
        "",
    )

    manufacturer = _first_present(
        root.get("manufacturer"),
        root.get("brand"),
        root.get("manufacturerName"),
        "",
    )

    manufacturer_part_number = _first_present(
        root.get("manufacturerPartNumber"),
        root.get("mpn"),
        root.get("mfrPartNumber"),
        "",
    )

    attributes: list[dict[str, str]] = []
    raw_attrs = _first_present(
        root.get("paramVOList"),
        root.get("attributes"),
        root.get("params"),
        root.get("properties"),
        [],
    )

    if isinstance(raw_attrs, dict):
        raw_attrs = [{"name": key, "value": value} for key, value in raw_attrs.items()]
    elif not isinstance(raw_attrs, Iterable):
        raw_attrs = []

    for item in raw_attrs:
        if isinstance(item, dict):
            field_name = _first_present(item.get("name"), item.get("paramName"), item.get("key"), item.get("label"))
            field_value = _first_present(item.get("value"), item.get("paramValue"), item.get("data"), item.get("text"), item.get("valueText"))
            if field_name is None:
                continue
            attributes.append({
                "name": _normalize_field_name(field_name),
                "value": _normalize_parameter_value(field_value),
            })

    # Try to extract a more general “attributes” list if the payload uses a nested dict
    for candidate_key in ("property", "properties", "attributeList"):
        nested = root.get(candidate_key)
        if isinstance(nested, dict):
            for key, value in nested.items():
                attributes.append({"name": _normalize_field_name(key), "value": _normalize_parameter_value(value)})

    return {
        "sku": str(sku),
        "name": _clean_text(name),
        "description": _clean_text(description),
        "category": _clean_text(category_name),
        "manufacturer": _clean_text(manufacturer),
        "manufacturer_part_number": _clean_text(manufacturer_part_number),
        "attributes": attributes,
    }


def build_category_path(category_name: str, root_path: str) -> str:
    """Append a remote category path to the configured InvenTree category root."""
    root_parts = [part.strip() for part in _clean_text(root_path).strip("/").split("/") if part.strip()]
    cleaned = _clean_text(category_name)
    if not cleaned or cleaned.lower() in {"uncategorized", "unknown", "none"}:
        return "/".join(root_parts)

    parts = [part.strip() for part in cleaned.replace("/", "|").replace("\\", "|").split("|")]
    filtered = [part for part in parts if part]
    if not filtered:
        return "/".join(root_parts)
    return "/".join([*root_parts, *filtered])
