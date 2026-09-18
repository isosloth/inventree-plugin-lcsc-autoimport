from __future__ import annotations

from decimal import Decimal, InvalidOperation

from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .adapter import normalize_lcsc_payload, parse_lcsc_order_csv
from .client import LCSCClient
from .service import import_lcsc_product

MAX_CSV_SIZE = 2 * 1024 * 1024


def _get_plugin():
    from plugin.registry import registry

    return registry.get_plugin("lcscautoimport")


def _parse_quantity(value):
    if value in (None, ""):
        return None
    try:
        quantity = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("quantity must be numeric") from exc
    if quantity <= 0:
        raise ValueError("quantity must be greater than zero")
    return quantity


def _run_import_items(items, request, *, category_override=None, supplier_override=None, api_overrides=None):
    """Shared import loop used by both the JSON bulk endpoint and the CSV upload endpoint."""
    plugin = _get_plugin()
    api_overrides = api_overrides or {}

    remote_client = LCSCClient(
        base_url=api_overrides.get("api_url") or (plugin.get_setting("LCSC_API_URL") if plugin else None),
        api_key=api_overrides.get("api_key") or (plugin.get_setting("LCSC_API_KEY") if plugin else None),
        send_auth_headers=plugin._setting_is_enabled("SEND_AUTH_HEADERS") if plugin else False,
        request_headers=plugin._request_headers() if plugin else {},
        timeout=int(api_overrides.get("timeout") or (plugin.get_setting("TIMEOUT_SECONDS") if plugin else 15) or 15),
        verify_ssl=bool(api_overrides.get("verify_ssl", True)),
    )

    supplier = supplier_override or (plugin._get_supplier() if plugin else None)
    stock_location = plugin._stock_location(request.user) if plugin else None
    image_headers = plugin._request_headers() if plugin else {}
    price_currency_symbol, price_currency_code = plugin._price_currency() if plugin else ("€", None)

    results = []
    for item in items:
        sku_hint = str(item.get("sku") or item.get("productCode") or "")
        try:
            quantity = _parse_quantity(item.get("quantity"))

            if "sku" in item and not item.get("attributes"):
                product = remote_client.fetch_product(item["sku"])
                if item.get("category"):
                    product["category"] = item.get("category")
            else:
                product = normalize_lcsc_payload(item)

            category_ids = None
            if category_override:
                category_path = category_override
            elif plugin is not None:
                category_path, category_ids = plugin._category_path_for_product(
                    product.get("category"),
                    product.get("category_chain"),
                    product.get("category_chain_ids"),
                )
            else:
                category_path = product.get("category")

            import_result = import_lcsc_product(
                product,
                supplier=supplier,
                category_path=category_path,
                category_ids=category_ids,
                quantity=quantity,
                stock_location=stock_location,
                image_headers=image_headers,
                price_currency_symbol=price_currency_symbol,
                price_currency_code=price_currency_code,
            )
            results.append({
                "sku": import_result["sku"],
                "status": "created" if import_result["result"] == "created" else "updated",
                "part_pk": import_result["part"].pk,
                "stock_item": import_result["stock_item"].pk if import_result["stock_item"] else None,
                "price_breaks": len(import_result.get("price_breaks") or []),
            })
        except Exception as exc:  # pragma: no cover - exercised via API tests
            results.append({"sku": sku_hint, "status": "failed", "error": str(exc)})

    summary = {
        "total": len(results),
        "created": sum(1 for r in results if r.get("status") == "created"),
        "updated": sum(1 for r in results if r.get("status") == "updated"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
    }
    return results, summary


class BulkImportAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        payload = request.data or {}

        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("items") or payload.get("skus") or []
            if isinstance(payload.get("sku"), str):
                items = [payload.get("sku")]
        else:
            return Response({"error": "Unsupported request format"}, status=status.HTTP_400_BAD_REQUEST)

        if not items:
            return Response({"error": "No LCSC items were supplied"}, status=status.HTTP_400_BAD_REQUEST)

        resolved_items = []
        for item in items:
            if isinstance(item, str):
                resolved_items.append({"sku": item})
            elif isinstance(item, dict):
                resolved_items.append(item)
            else:
                resolved_items.append({"sku": str(item)})

        results, summary = _run_import_items(
            resolved_items,
            request,
            category_override=request.data.get("category_path"),
            supplier_override=request.data.get("supplier"),
            api_overrides={
                "api_url": request.data.get("api_url"),
                "api_key": request.data.get("api_key"),
                "timeout": request.data.get("timeout"),
                "verify_ssl": request.data.get("verify_ssl", True),
            },
        )

        return Response({"results": results, "summary": summary}, status=status.HTTP_200_OK)


class CSVImportAPIView(APIView):
    """Upload an LCSC order/BOM CSV export and import every listed part.

    Accepts either a multipart file upload (field name ``file``) or a JSON body with a
    ``csv`` string field containing the raw CSV text. The CSV must have a header row; common
    LCSC export column names (e.g. "LCSC Part Number", "Order Qty.") are recognized
    automatically, alongside simpler "SKU" / "Quantity" headers.
    """

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, *args, **kwargs):
        uploaded_file = request.FILES.get("file")
        if uploaded_file is not None:
            if uploaded_file.size > MAX_CSV_SIZE:
                return Response({"error": "CSV file is larger than 2 MiB"}, status=status.HTTP_400_BAD_REQUEST)
            try:
                csv_text = uploaded_file.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                return Response({"error": "CSV file must be UTF-8 encoded"}, status=status.HTTP_400_BAD_REQUEST)
        else:
            csv_text = (request.data or {}).get("csv")

        if not csv_text or not str(csv_text).strip():
            return Response(
                {"error": "Upload a CSV file (field 'file') or provide raw CSV text (field 'csv')"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            rows = parse_lcsc_order_csv(str(csv_text))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        items = [{"sku": row["sku"], "quantity": row.get("quantity")} for row in rows]

        results, summary = _run_import_items(
            items,
            request,
            category_override=request.data.get("category_path") if hasattr(request.data, "get") else None,
        )

        return Response({"results": results, "summary": summary}, status=status.HTTP_200_OK)
