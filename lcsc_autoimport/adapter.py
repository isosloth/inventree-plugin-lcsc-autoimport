import logging
import re
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


def _clean_chain(values: Iterable[Any]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        text = _clean_text(value)
        if text and text.lower() not in {"uncategorized", "unknown", "none"}:
            cleaned.append(text)
    return cleaned


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


def _slugify_key(name: Any) -> str:
    """Build a stable, uppercase parameter key from a human-readable name."""
    text = _clean_text(name).upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text).strip("_")
    return text or "PARAM"


def _looks_like_encapsulation(name: Any) -> bool:
    text = _clean_text(name).lower()
    return "encap" in text or "package" in text or text in {"case", "case/package"}


def _parse_encapsulation(raw: Any, source_id: str = "") -> list[dict[str, str]]:
    """Split a raw LCSC encapsulation value into mounting type and package parameters.

    LCSC commonly reports this as ``"SMD,SOD-123"`` or ``"Through Hole,TO-92"``. When no
    comma is present (e.g. ``"Plugin"``), it is treated as the package value on its own.
    """
    cleaned = _clean_text(raw)
    if not cleaned:
        return []

    if "," in cleaned:
        mounting_raw, package_raw = (part.strip() for part in cleaned.split(",", 1))
    else:
        mounting_raw, package_raw = "", cleaned

    entries: list[dict[str, str]] = []

    if mounting_raw:
        mounting_lower = mounting_raw.lower()
        if "smd" in mounting_lower or "smt" in mounting_lower:
            mounting = "SMD"
        elif "through hole" in mounting_lower or "tht" in mounting_lower or "dip" in mounting_lower:
            mounting = "THT"
        else:
            mounting = mounting_raw if len(mounting_raw) <= 5 else mounting_raw.title()
        entries.append({"id": "MOUNTING_TYPE", "name": "Mounting Type", "value": mounting})

    if package_raw:
        entries.append({
            "id": _clean_text(source_id) or "ENCAPSULATION",
            "name": "Package / Encapsulation",
            "value": package_raw,
        })

    return entries


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
	root.get("title"),
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
        root.get("brandNameEn"),
        root.get("manufacturerName"),
        "",
    )

    manufacturer_part_number = _first_present(
        root.get("manufacturerPartNumber"),
	root.get("productModel"),
        root.get("mpn"),
        root.get("mfrPartNumber"),
        "",
    )

    parent_catalog_list = root.get("parentCatalogList")
    if not isinstance(parent_catalog_list, list):
        parent_catalog_list = []
    chain_pairs: list[tuple[Any, Any]] = [
        (_first_present(entry.get("catalogNameEn"), entry.get("catalogName")), entry.get("catalogId"))
        for entry in parent_catalog_list
        if isinstance(entry, dict)
    ]
    chain_pairs.append((root.get("parentCatalogName"), root.get("wmCatalogId")))

    category_chain: list[str] = []
    category_chain_ids: list[str] = []
    for chain_name, chain_id in chain_pairs:
        cleaned_name = _clean_text(chain_name)
        if not cleaned_name or cleaned_name.lower() in {"uncategorized", "unknown", "none"}:
            continue
        category_chain.append(cleaned_name)
        category_chain_ids.append(_clean_text(chain_id))
    if category_chain:
        category_name = category_chain[-1]

    pdf_url = _first_present(
        root.get("pdfUrl"),
        root.get("datasheetUrl"),
        "",
    )
    product_url = _first_present(
        root.get("productUrl"),
        root.get("productDetailUrl"),
        root.get("detailUrl"),
        f"https://www.lcsc.com/product-detail/{sku}.html",
    )
    product_images = root.get("productImages") or []
    first_image = product_images[0] if isinstance(product_images, list) and product_images else ""
    if isinstance(first_image, dict):
        first_image = _first_present(
            first_image.get("url"),
            first_image.get("imageUrl"),
            first_image.get("src"),
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

    encapsulation_added = False

    for item in raw_attrs:
        if isinstance(item, dict):
            field_id = _clean_text(_first_present(item.get("paramId"), item.get("id"), item.get("attributeId")))
            field_name = _first_present(item.get("name"), item.get("paramNameEn"), item.get("paramName"), item.get("label"))
            field_value = _first_present(item.get("value"), item.get("paramValueEn"), item.get("paramValue"), item.get("text"), item.get("valueText"))
            if field_name is None:
                continue
            display_name = _normalize_field_name(field_name)
            raw_value = _normalize_parameter_value(field_value)
            if _looks_like_encapsulation(field_name):
                parsed = _parse_encapsulation(raw_value, field_id)
                if parsed:
                    attributes.extend(parsed)
                    encapsulation_added = True
                    continue
            attributes.append({
                "id": field_id or _slugify_key(display_name),
                "name": display_name,
                "value": raw_value,
            })

    # Try to extract a more general “attributes” list if the payload uses a nested dict
    for candidate_key in ("property", "properties", "attributeList"):
        nested = root.get(candidate_key)
        if isinstance(nested, dict):
            for key, value in nested.items():
                display_name = _normalize_field_name(key)
                raw_value = _normalize_parameter_value(value)
                if _looks_like_encapsulation(key):
                    parsed = _parse_encapsulation(raw_value)
                    if parsed:
                        attributes.extend(parsed)
                        encapsulation_added = True
                        continue
                attributes.append({
                    "id": _slugify_key(display_name),
                    "name": display_name,
                    "value": raw_value,
                })

    if not encapsulation_added:
        fallback_encap = _first_present(
            root.get("encapStandard"),
            root.get("encapsulationStandard"),
            root.get("encapsulation"),
            root.get("packageType"),
            root.get("footprint"),
            "",
        )
        attributes.extend(_parse_encapsulation(fallback_encap))

    return {
        "sku": str(sku),
        "name": _clean_text(name),
        "description": _clean_text(description),
        "category": _clean_text(category_name),
        "category_chain": category_chain,
        "category_chain_ids": category_chain_ids,
        "manufacturer": _clean_text(manufacturer),
        "manufacturer_part_number": _clean_text(manufacturer_part_number),
        "pdf_url": _clean_text(pdf_url),
        "product_url": _clean_text(product_url),
        "image_url": _clean_text(first_image),
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


def build_category_chain_path(category_chain: list[str], root_path: str) -> str:
    """Build a nested InvenTree category path from an ordered list of remote category names."""
    root_parts = [part.strip() for part in _clean_text(root_path).strip("/").split("/") if part.strip()]

    filtered: list[str] = []
    for name in category_chain or []:
        cleaned = _clean_text(name)
        if not cleaned or cleaned.lower() in {"uncategorized", "unknown", "none"}:
            continue
        for piece in cleaned.replace("\\", "/").split("/"):
            piece = piece.strip()
            if piece:
                filtered.append(piece)

    if not filtered:
        return "/".join(root_parts)
    return "/".join([*root_parts, *filtered])
