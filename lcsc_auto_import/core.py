from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from plugin import InvenTreePlugin
from plugin.mixins import BarcodeMixin, SettingsMixin, UrlsMixin

from .version import PLUGIN_VERSION
from .adapter import build_category_chain_path, build_category_path
from .client import LCSCClient
from .service import import_lcsc_product, resolve_lcsc_supplier
from .urls import urlpatterns as PLUGIN_URLPATTERNS

logger = logging.getLogger(__name__)


class LCSCAutoImport(SettingsMixin, BarcodeMixin, UrlsMixin, InvenTreePlugin):
    NAME = "LCSCAutoImport"
    SLUG = "lcscautoimport"
    TITLE = "LCSC Auto Import"
    DESCRIPTION = "Import LCSC parts automatically from product JSON and scanned QR payloads"
    VERSION = PLUGIN_VERSION
    AUTHOR = "isosloth"

    SETTINGS = {
        "LCSC_API_URL": {
            "name": "LCSC API URL",
            "description": "URL used to fetch product metadata for a given LCSC code",
            "default": "https://wmsc.lcsc.com/ftps/wm/product/detail",
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
        "CATEGORY_ROOT_PATH": {
            "name": "Category Root Path",
            "description": "All LCSC categories are created below this InvenTree category path",
            "default": "Electronics/PCB-Parts",
        },
        "DEFAULT_CATEGORY_PATH": {
            "name": "Default Category Path",
            "description": "Fallback category name, relative to Category Root Path",
            "default": "Uncategorized",
        },
        "CATEGORY_MAPPING": {
            "name": "Category Mapping",
            "description": "JSON mapping of remote category names to paths relative to Category Root Path",
            "default": "{}",
        },
        "SEND_AUTH_HEADERS": {
            "name": "Send Authentication Headers",
            "description": "Send Authorization and X-API-Key headers when an API key is configured",
            "default": False,
            "type": "bool",
        },
        "REQUEST_HEADERS": {
            "name": "Request Headers",
            "description": "JSON object of additional HTTP headers sent with product requests",
            "default": "{}",
        },
        "DEFAULT_STOCK_LOCATION": {
            "name": "Default Stock Location",
            "description": (
                "Fallback location used when an LCSC QR code includes a quantity and the "
                "scanning user has not chosen their own location (see the user's account "
                "settings for a per-user override)"
            ),
            "model": "stock.stocklocation",
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
        "PRICE_CURRENCY_SYMBOL": {
            "name": "Price Currency Symbol",
            "description": (
                "Only price breaks reported by LCSC under this currency symbol are imported "
                "as Supplier Price Breaks (LCSC reports the same ladder in multiple currencies)"
            ),
            "default": "€",
        },
        "PRICE_CURRENCY_CODE": {
            "name": "Price Currency Code",
            "description": (
                "ISO currency code used when saving price breaks; leave blank to auto-detect "
                "from Price Currency Symbol (€ -> EUR, $ -> USD, £ -> GBP, ¥ -> CNY)"
            ),
            "default": "",
        },
    }

    USER_SETTINGS = {
        "DEFAULT_STOCK_LOCATION": {
            "name": "Default Stock Location",
            "description": "Location used when you scan an LCSC QR code that includes a quantity",
            "model": "stock.stocklocation",
        },
        "OVERRIDE_QUANTITY": {
            "name": "Override Quantity",
            "description": "Allow the user to override the quantity specified in the LCSC QR code",
            "default": -1,
            "type": "number",
        }
    }

    URLS = PLUGIN_URLPATTERNS

    LCSC_QR_RE = re.compile(r".*pc:([^,}]+).*", re.IGNORECASE)
    LCSC_QUANTITY_RE = re.compile(r"(?:^|[,{\s])(?:qty|quantity):([^,}]+)", re.IGNORECASE)

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

    def _setting_is_enabled(self, key: str) -> bool:
        value = self.get_setting(key)
        return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _request_headers(self) -> dict[str, str]:
        raw_headers = self.get_setting("REQUEST_HEADERS") or "{}"
        try:
            headers = json.loads(raw_headers)
        except json.JSONDecodeError as exc:
            raise ValidationError("Request Headers must be a valid JSON object") from exc

        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in headers.items()
        ):
            raise ValidationError("Request Headers must map string header names to string values")

        return headers

    def _category_path_for_product(
        self,
        product_category: str | None,
        category_chain: list[str] | None = None,
        category_chain_ids: list[str] | None = None,
    ) -> tuple[str, list[str] | None]:
        """Resolve the InvenTree category path (and, where applicable, source catalog ids).

        Returns a ``(path, category_ids)`` tuple. ``category_ids`` is only populated when the
        path was built directly from the remote category chain (i.e. no ``Category Mapping``
        override applied), since a manual mapping no longer corresponds 1:1 with remote ids.
        """
        root_path = self.get_setting("CATEGORY_ROOT_PATH") or "Electronics/PCB-Parts"
        mapping = self._category_mapping()
        lookup_name = product_category or (category_chain[-1] if category_chain else None)
        if lookup_name:
            for src, target in mapping.items():
                if str(src).lower() == str(lookup_name).lower():
                    return build_category_path(str(target), root_path), None
        if category_chain:
            return build_category_chain_path(category_chain, root_path), category_chain_ids
        if product_category:
            return build_category_path(product_category, root_path), None
        default = self.get_setting("DEFAULT_CATEGORY_PATH") or "Uncategorized"
        return build_category_path(default, root_path), None

    def _price_currency(self) -> tuple[str, str | None]:
        symbol = self.get_setting("PRICE_CURRENCY_SYMBOL")
        symbol = symbol if symbol is not None else "€"
        code = (self.get_setting("PRICE_CURRENCY_CODE") or "").strip() or None
        return symbol, code

    def _stock_location(self, user=None):
        if user is not None and getattr(user, "is_authenticated", False):
            user_location = self.get_user_setting("DEFAULT_STOCK_LOCATION", user)
            if user_location:
                return user_location
        return self.get_setting("DEFAULT_STOCK_LOCATION")

    def _qr_quantity(self, barcode_data: str, user=None):
        match = self.LCSC_QUANTITY_RE.search(barcode_data)
        if not match:
            return None

        try:
            quantity = Decimal(match.group(1).strip())
        except InvalidOperation as exc:
            raise ValidationError("LCSC QR quantity must be numeric") from exc

        if quantity <= 0:
            raise ValidationError("LCSC QR quantity must be greater than zero")

        override = self.get_user_setting("OVERRIDE_QUANTITY", user) if user else None
        if override not in (None, "", -1, "-1"):
            try:
                quantity = Decimal(str(override))
            except (InvalidOperation, TypeError):
                raise ValidationError("Override quantity must be numeric") from None
            if quantity < 0:
                raise ValidationError("Override quantity must be greater or equal to zero")

        return quantity

    def import_lcsc_sku(self, sku: str, *, product_payload: dict | None = None, quantity=None, user=None):
        if not sku:
            raise ValidationError("SKU is required")

        supplier = self._get_supplier()
        if supplier is None:
            raise ValidationError("No LCSC supplier company could be resolved")

        if product_payload is None and self.get_setting("FETCH_ENABLED"):
            client = LCSCClient(
                base_url=self.get_setting("LCSC_API_URL"),
                api_key=self.get_setting("LCSC_API_KEY"),
                send_auth_headers=self._setting_is_enabled("SEND_AUTH_HEADERS"),
                request_headers=self._request_headers(),
                timeout=int(self.get_setting("TIMEOUT_SECONDS") or 15),
            )
            product_payload = client.fetch_product(sku)
        elif product_payload is None:
            product_payload = {"sku": sku, "category": None, "attributes": []}

        category_path, category_ids = self._category_path_for_product(
            product_payload.get("category"),
            product_payload.get("category_chain"),
            product_payload.get("category_chain_ids"),
        )
        price_currency_symbol, price_currency_code = self._price_currency()
        return import_lcsc_product(
            product_payload,
            supplier=supplier,
            category_path=category_path,
            category_ids=category_ids,
            quantity=quantity,
            stock_location=self._stock_location(user),
            image_headers=self._request_headers(),
            price_currency_symbol=price_currency_symbol,
            price_currency_code=price_currency_code,
        )

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
            quantity = self._qr_quantity(barcode_data, user=user)
            result = self.import_lcsc_sku(sku, quantity=quantity, user=user)
        except Exception as exc:  # pragma: no cover - exercised by mocked tests
            logger.exception("LCSC scan auto-import failed for %s", sku)
            return {"error": str(exc)}

        part = result["part"]
        return {
            "part": part.format_matched_response(user=user),
            "success": "Found matching LCSC part",
            "sku": sku,
            "supplierpart": {"pk": result["supplier_part"].pk},
            "quantity": str(quantity) if quantity is not None else None,
            "stock_item": result["stock_item"].pk if result["stock_item"] else None,
            "price_breaks": len(result.get("price_breaks") or []),
        }
