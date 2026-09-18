import logging
from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import requests
from django.core.files.base import ContentFile
from django.db import transaction

from company.models import Company, ManufacturerPart, SupplierPart, SupplierPriceBreak
from stock.models import StockItem, StockLocation
from common.models import Parameter, ParameterTemplate
from part.models import Part, PartCategory, PartCategoryParameterTemplate

logger = logging.getLogger(__name__)


class LCSCImportError(RuntimeError):
    pass


CURRENCY_SYMBOL_MAP = {
    "€": "EUR",
    "$": "USD",
    "£": "GBP",
    "¥": "CNY",
    "₩": "KRW",
}


def _sync_supplier_pricing(
    supplier_part: SupplierPart,
    price_breaks: list[dict[str, Any]] | None,
    *,
    currency_symbol: str = "€",
    currency_code: str | None = None,
) -> list[SupplierPriceBreak]:
    """Create/update ``SupplierPriceBreak`` rows from the remote price ladder.

    Only entries whose reported ``currency_symbol`` matches ``currency_symbol`` are imported
    (LCSC's product endpoint commonly reports the same ladder in multiple currencies). Set
    ``currency_symbol`` to an empty string to import every reported entry as-is.
    """
    if not price_breaks:
        return []

    resolved_currency = currency_code or CURRENCY_SYMBOL_MAP.get(currency_symbol, currency_symbol or "EUR")

    synced: list[SupplierPriceBreak] = []
    for entry in price_breaks:
        if currency_symbol and entry.get("currency_symbol") != currency_symbol:
            continue
        quantity = entry.get("quantity")
        price = entry.get("price")
        if quantity is None or price is None:
            continue
        price_break, _ = SupplierPriceBreak.objects.update_or_create(
            part=supplier_part,
            quantity=quantity,
            defaults={"price": price, "price_currency": resolved_currency},
        )
        synced.append(price_break)
    return synced


def get_default_supplier() -> Company | None:
    return Company.objects.filter(is_supplier=True, name__icontains="LCSC").first()


def ensure_category_path(
    category_path: str,
    *,
    parent: PartCategory | None = None,
    category_ids: list[str] | None = None,
) -> PartCategory:
    """Create (or fetch) the full category tree for a slash-separated path.

    ``category_ids`` (if provided) holds the LCSC catalog id for the *trailing* segments of
    the path (i.e. the segments sourced from the remote category chain, not the configured
    root path) and is aligned by matching from the end of ``path``. When a segment is newly
    created, its matching id is stashed in category metadata for traceability.
    """
    path = [segment.strip() for segment in category_path.split("/") if segment and segment.strip()]
    if not path:
        raise ValueError("Category path is empty")

    ids = list(category_ids or [])
    offset = len(path) - len(ids)

    current = parent
    for index, segment in enumerate(path):
        current, created = PartCategory.objects.get_or_create(name=segment, parent=current)
        if created and 0 <= index - offset < len(ids):
            segment_id = str(ids[index - offset] or "").strip()
            if segment_id:
                current.set_metadata("lcsc_catalog_id", segment_id)
    return current


def normalize_template_name(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return "Unnamed Parameter"
    return text.replace("_", " ").replace("-", " ")


def create_or_update_parameter(
    part: Part, parameter_id: str, display_name: str, value: Any
) -> ParameterTemplate:
    """Create/update a parameter using a stable ``parameter_id`` as the template name.

    LCSC's human-readable parameter names (``paramNameEn``) can vary between categories for
    what is otherwise the same underlying attribute, while ``paramId`` (or a slug derived from
    the name, when no id is supplied) is stable. The template's ``name`` therefore stores the
    stable id, while ``display_name`` is kept in the template description for UI purposes.
    """
    parameter_id = (parameter_id or "").strip() or normalize_template_name(display_name).upper().replace(" ", "_") or "PARAM"
    display_name = normalize_template_name(display_name) or parameter_id

    template, created = ParameterTemplate.objects.get_or_create(
        name=parameter_id,
        defaults={"description": display_name},
    )
    if not created and display_name and template.description != display_name:
        template.description = display_name
        template.save(update_fields=["description"])

    category_templates = PartCategoryParameterTemplate.objects.filter(category=part.category)
    if not category_templates.filter(template=template).exists():
        PartCategoryParameterTemplate.objects.get_or_create(
            category=part.category,
            template=template,
            defaults={"default_value": ""},
        )

    existing = part.parameters_list.filter(template=template).first()
    if existing:
        existing.data = str(value)
        existing.save(update_fields=["data"])
    else:
        Parameter.objects.create(
            model_type=part.get_content_type(),
            model_id=part.pk,
            template=template,
            data=str(value),
        )
    return template


def resolve_lcsc_supplier(supplier: Company | int | str | None) -> Company | None:
    if supplier is None:
        return get_default_supplier()
    if isinstance(supplier, Company):
        return supplier
    if isinstance(supplier, int):
        return Company.objects.filter(pk=supplier, is_supplier=True).first()
    if isinstance(supplier, str):
        supplier = supplier.strip()
        if not supplier:
            return get_default_supplier()
        return Company.objects.filter(is_supplier=True, name__icontains=supplier).first()
    return None


def _store_product_image(part: Part, image_url: str, headers: Mapping[str, str] | None) -> None:
    if part.image or not image_url:
        return
    parsed = urlparse(image_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LCSCImportError("Product image URL must use HTTP or HTTPS")
    response = requests.get(image_url, headers=headers, timeout=15)
    response.raise_for_status()
    if not response.headers.get("Content-Type", "").lower().startswith("image/"):
        raise LCSCImportError("Product image URL did not return an image")
    content = response.content
    if len(content) > 5 * 1024 * 1024:
        raise LCSCImportError("Product image is larger than 5 MiB")
    filename = PurePosixPath(parsed.path).name or f"lcsc-{part.pk}.jpg"
    part.image.save(filename, ContentFile(content), save=True)


def _add_stock(part: Part, supplier_part: SupplierPart, quantity: Decimal | None, stock_location) -> StockItem | None:
    if quantity is None:
        return None
    location = StockLocation.objects.filter(pk=stock_location).first()
    if location is None:
        raise LCSCImportError("Configure Default Stock Location before scanning a quantity")
    stock_item = StockItem.objects.filter(part=part, location=location).order_by("pk").first()
    if stock_item is None:
        return StockItem.objects.create(part=part, supplier_part=supplier_part, location=location, quantity=quantity)
    stock_item.quantity += quantity
    stock_item.save()
    return stock_item


def import_lcsc_product(
    product: dict[str, Any],
    *,
    supplier: Company | int | str | None,
    category_path: str | None = None,
    category_ids: list[str] | None = None,
    quantity: Decimal | None = None,
    stock_location=None,
    image_headers: Mapping[str, str] | None = None,
    price_currency_symbol: str = "€",
    price_currency_code: str | None = None,
) -> dict[str, Any]:
    """Creates or updates a Part and SupplierPart for an LCSC product payload."""
    if not isinstance(product, dict):
        raise LCSCImportError("Product payload is not a dictionary")

    supplier_obj = resolve_lcsc_supplier(supplier)
    if supplier_obj is None:
        raise LCSCImportError("No LCSC supplier company configured")

    sku = str(product.get("sku") or "").strip()
    if not sku:
        raise LCSCImportError("LCSC product payload is missing a SKU")

    category_name = str(product.get("category") or "Uncategorized").strip()
    resolved_category_path = category_path or category_name
    if not resolved_category_path:
        resolved_category_path = "Electronics/Uncategorized"

    category = ensure_category_path(resolved_category_path, category_ids=category_ids)

    supplier_part = SupplierPart.objects.filter(SKU=sku).select_related("part").first()
    part = supplier_part.part if supplier_part else None
    manufacturer_part_number = str(product.get("manufacturer_part_number") or "").strip()
    manufacturer_part = None
    if part is None and manufacturer_part_number:
        manufacturer_part = ManufacturerPart.objects.filter(MPN=manufacturer_part_number).select_related("part").first()
        part = manufacturer_part.part if manufacturer_part else None

    if part is None:
        part = Part.objects.create(
            name=product.get("name") or sku,
            description=product.get("description") or "",
            category=category,
            purchaseable=True,
            component=True,
            active=True,
            link=product.get("pdf_url") or None,
        )
        create_result = "created"
    else:
        part.name = product.get("name") or part.name
        part.description = product.get("description") or part.description
        part.category = category
        part.link = product.get("pdf_url") or part.link
        part.save(update_fields=["name", "description", "category", "link"])
        create_result = "updated"

    if supplier_part is None:
        supplier_part = SupplierPart.objects.create(
            SKU=sku,
            supplier=supplier_obj,
            part=part,
            link=product.get("product_url") or None,
        )
    else:
        supplier_part.part = part
        supplier_part.supplier = supplier_obj
        supplier_part.link = product.get("product_url") or supplier_part.link
        supplier_part.save(update_fields=["part", "supplier", "link"])

    manufacturer_name = (product.get("manufacturer") or "").strip()
    manufacturer_part_number = (product.get("manufacturer_part_number") or "").strip()
    if manufacturer_name:
        manufacturer = Company.objects.filter(name__icontains=manufacturer_name, is_manufacturer=True).first()
        if manufacturer is None:
            manufacturer = Company.objects.create(name=manufacturer_name, is_manufacturer=True)
        if manufacturer_part_number:
            manufacturer_part = ManufacturerPart.objects.filter(
                manufacturer=manufacturer,
                part=part,
                MPN=manufacturer_part_number,
            ).first()
            if manufacturer_part is None:
                ManufacturerPart.objects.create(
                    manufacturer=manufacturer,
                    part=part,
                    MPN=manufacturer_part_number,
                )

    for attribute in product.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        display_name = str(attribute.get("name") or "").strip()
        parameter_id = str(attribute.get("id") or "").strip()
        value = attribute.get("value")
        if not display_name and not parameter_id:
            continue
        create_or_update_parameter(part, parameter_id, display_name, value)

    _store_product_image(part, str(product.get("image_url") or ""), image_headers)
    stock_item = _add_stock(part, supplier_part, quantity, stock_location)
    price_breaks = _sync_supplier_pricing(
        supplier_part,
        product.get("price_breaks"),
        currency_symbol=price_currency_symbol,
        currency_code=price_currency_code,
    )

    return {
        "sku": sku,
        "part": part,
        "supplier_part": supplier_part,
        "stock_item": stock_item,
        "category": category,
        "price_breaks": price_breaks,
        "result": create_result,
        "warnings": [],
    }


def bulk_import_lcsc_items(items: Iterable[str | dict[str, Any]], *, supplier: Company | int | str | None, category_path: str | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            sku = item.strip()
            result = {"sku": sku, "status": "skipped"}
            if not sku:
                result["error"] = "Empty SKU"
                results.append(result)
                continue
            try:
                product = {"sku": sku, "category": category_path or "Electronics/Uncategorized", "attributes": []}
                import_result = import_lcsc_product(product, supplier=supplier, category_path=category_path)
                result["status"] = "created" if import_result["result"] == "created" else "updated"
                result["part_pk"] = import_result["part"].pk
            except Exception as exc:  # pragma: no cover - exercised in mocked tests
                logger.exception("Failed to import LCSC SKU %s", sku)
                result["error"] = str(exc)
                result["status"] = "failed"
            results.append(result)
            continue

        if isinstance(item, dict):
            try:
                import_result = import_lcsc_product(item, supplier=supplier, category_path=category_path)
                result = {
                    "sku": import_result["sku"],
                    "status": "created" if import_result["result"] == "created" else "updated",
                    "part_pk": import_result["part"].pk,
                }
            except Exception as exc:
                logger.exception("Failed to import LCSC payload: %s", item)
                result = {"sku": str(item.get("sku") or item.get("productCode") or ""), "status": "failed", "error": str(exc)}
            results.append(result)
            continue

        results.append({"status": "failed", "error": "Unsupported item type"})

    return {"results": results, "summary": {"total": len(results), "created": sum(1 for r in results if r.get("status") == "created"), "updated": sum(1 for r in results if r.get("status") == "updated"), "failed": sum(1 for r in results if r.get("status") == "failed"), "skipped": sum(1 for r in results if r.get("status") == "skipped")}}
