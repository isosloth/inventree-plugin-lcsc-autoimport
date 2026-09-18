from __future__ import annotations

import json
import logging
import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from plugin import InvenTreePlugin
from plugin.mixins import BarcodeMixin, SettingsMixin, UrlsMixin

from .adapter import build_category_path
from .client import LCSCClient
from .service import import_lcsc_product, resolve_lcsc_supplier

logger = logging.getLogger(__name__)


class LCSCAutoImportPlugin(SettingsMixin, BarcodeMixin, UrlsMixin, InvenTreePlugin):
    NAME = "LCSCAutoImport"
    SLUG = "lcscautoimport"
    TITLE = "LCSC Auto Import"
    DESCRIPTION = "Import LCSC parts automatically from product JSON and scanned QR payloads"
    VERSION = "0.1.0"
    AUTHOR = "isosloth"

    SETTINGS = {
        "LCSC_API_URL": {
            "name": "LCSC API URL",
            "description": "URL used to fetch product metadata for a given LCSC code",
            "default": "https://wmsc.lcsc.com/wmsc/product/detail",
        },
        "LCSC_API_KEY": {
            "name": "LCSC API Key",
            "description": "Optional bearer/API token used for the configured product endpoint",
            "protected": True,
        },
        "LCSC_SUPPLIER_ID": {
            "name": "LCSC Supplier",
            "description": "The supplier company record used for LCSC parts",
            "model": "company.company",
            "model_filters": {"is_supplier": True},
        },
        "DEFAULT_CATEGORY_PATH": {
            "name": "Default Category Path",
            "description": "Fallback category path used when no category can be resolved",
            "default": "Electronics/Uncategorized",
        },
        "CATEGORY_MAPPING": {
            "name": "Category Mapping",
            "description": "JSON mapping of remote category names to local InvenTree category paths",
            "default": "{}",
        },
        "FETCH_ENABLED": {
            "name": "Fetch Enabled",
            "description": "Toggle remote product fetching when importing an LCSC code",
            "default": True,
            "type": "bool",
        },
        "TIMEOUT_SECONDS": {
            "name": "Timeout Seconds",
            "description": "Request timeout in seconds for remote product fetches",
            "default": 15,
        },
    }

    URLS = [
        "lcsc_autoimport.urls",
    ]

    LCSC_QR_RE = re.compile(r".*pc:([^,}]+).*", re.IGNORECASE)

    def _get_supplier(self):
        supplier_id = self.get_setting("LCSC_SUPPLIER_ID")
        supplier = resolve_lcsc_supplier(supplier_id)
        if supplier is not None:
            return supplier
        return resolve_lcsc_supplier("LCSC")

    def _category_mapping(self):
        raw_map = self.get_setting("CATEGORY_MAPPING") or "{}"
        try:
            mapping = json.loads(raw_map)
        except json.JSONDecodeError:
            return {}
        return mapping if isinstance(mapping, dict) else {}

    def _category_path_for_product(self, product_category: str | None):
        mapping = self._category_mapping()
        if product_category:
            for src, target in mapping.items():
                if str(src).lower() == str(product_category).lower():
                    return str(target)
        default = self.get_setting("DEFAULT_CATEGORY_PATH") or "Electronics/Uncategorized"
        if product_category:
            return build_category_path(product_category, default)
        return default

    def import_lcsc_sku(self, sku: str, *, product_payload: dict | None = None):
        if not sku:
            raise ValidationError("SKU is required")

        supplier = self._get_supplier()
        if supplier is None:
            raise ValidationError("No LCSC supplier company could be resolved")

        if product_payload is None and self.get_setting("FETCH_ENABLED"):
            client = LCSCClient(
                base_url=self.get_setting("LCSC_API_URL"),
                api_key=self.get_setting("LCSC_API_KEY"),
                timeout=int(self.get_setting("TIMEOUT_SECONDS") or 15),
            )
            product_payload = client.fetch_product(sku)
        elif product_payload is None:
            product_payload = {"sku": sku, "category": self.get_setting("DEFAULT_CATEGORY_PATH") or "Electronics/Uncategorized", "attributes": []}

        category_path = self._category_path_for_product(product_payload.get("category"))
        return import_lcsc_product(product_payload, supplier=supplier, category_path=category_path)

    def scan(self, barcode_data: str, user, **kwargs):
        if not isinstance(barcode_data, str):
            return None

        match = self.LCSC_QR_RE.search(barcode_data.strip())
        if not match:
            return None

        sku = match.group(1).strip()
        if not sku:
            return None

        try:
            result = self.import_lcsc_sku(sku)
        except Exception as exc:  # pragma: no cover - exercised by mocked tests
            logger.exception("LCSC scan auto-import failed for %s", sku)
            return {"error": str(exc)}

        part = result["part"]
        return {
            "part": part.format_matched_response(user=user),
            "success": "Found matching LCSC part",
            "sku": sku,
            "supplierpart": {"pk": result["supplier_part"].pk},
        }
