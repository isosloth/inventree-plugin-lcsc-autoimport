import logging
from collections.abc import Mapping
from typing import Any

import requests

from .adapter import normalize_lcsc_payload

logger = logging.getLogger(__name__)


class LCSCClient:
    """Minimal LCSC-compatible client with a configurable endpoint."""

    DEFAULT_URL = "https://wmsc.lcsc.com/wmsc/product/detail"

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, timeout: int = 15, verify_ssl: bool = True):
        self.base_url = (base_url or self.DEFAULT_URL).strip()
        self.api_key = api_key
        self.timeout = timeout
        self.verify_ssl = verify_ssl

    def fetch_product(self, sku: str) -> dict[str, Any]:
        sku = str(sku).strip()
        if not sku:
            raise ValueError("SKU is required")

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key

        params = {"productCode": sku}
        try:
            response = requests.get(
                self.base_url,
                params=params,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
        except requests.RequestException as exc:  # pragma: no cover - exercised via mocked tests
            logger.exception("LCSC client request failed for SKU %s", sku)
            raise RuntimeError(f"Failed to fetch LCSC product {sku}: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - exercised via mocked tests
            raise RuntimeError(f"LCSC endpoint returned invalid JSON for SKU {sku}") from exc

        if not isinstance(payload, Mapping):
            raise RuntimeError(f"LCSC endpoint returned unexpected payload type for SKU {sku}")

        return normalize_lcsc_payload(dict(payload))
