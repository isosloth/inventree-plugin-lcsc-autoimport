import logging
from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests
from django.core.files.base import ContentFile
from django.db import transaction

from company.models import Company, ManufacturerPart, SupplierPart
from stock.models import StockItem, StockLocation
from common.models import Parameter, ParameterTemplate
from part.models import Part, PartCategory, PartCategoryParameterTemplate

logger = logging.getLogger(__name__)


class LCSCImportError(RuntimeError):
    pass


def get_default_supplier() -> Company | None:
    return Company.objects.filter(is_supplier=True, name__icontains="LCSC").first()


def ensure_category_path(category_path: str, *, parent: PartCategory | None = None) -> PartCategory:
    path = [segment.strip() for segment in category_path.split("/") if segment and segment.strip()]
    if not path:
        raise ValueError("Category path is empty")

    current = parent
    for segment in path:
        current, _ = PartCategory.objects.get_or_create(name=segment, parent=current)
    return current


def normalize_template_name(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return "Unnamed Parameter"
    return text.replace("_", " ").replace("-", " ")


def create_or_update_parameter(part: Part, parameter_name: str, value: Any) -> ParameterTemplate:
    template, _ = ParameterTemplate.objects.get_or_create(name=normalize_template_name(parameter_name))

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
    quantity: Decimal | None = None,
    stock_location=None,
    image_headers: Mapping[str, str] | None = None,
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

    category = ensure_category_path(resolved_category_path)

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
        name = str(attribute.get("name") or "").strip()
        value = attribute.get("value")
        if not name:
            continue
        create_or_update_parameter(part, name, value)

    _store_product_image(part, str(product.get("image_url") or ""), image_headers)
    stock_item = _add_stock(part, supplier_part, quantity, stock_location)

    return {
        "sku": sku,
        "part": part,
        "supplier_part": supplier_part,
        "stock_item": stock_item,
        "category": category,
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
