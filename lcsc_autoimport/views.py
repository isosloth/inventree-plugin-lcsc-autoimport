from __future__ import annotations

from decimal import Decimal, InvalidOperation

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .adapter import normalize_lcsc_payload
from .client import LCSCClient
from .service import import_lcsc_product


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

        plugin = _get_plugin()

        remote_client = LCSCClient(
            base_url=request.data.get("api_url") or (plugin.get_setting("LCSC_API_URL") if plugin else None),
            api_key=request.data.get("api_key") or (plugin.get_setting("LCSC_API_KEY") if plugin else None),
            send_auth_headers=plugin._setting_is_enabled("SEND_AUTH_HEADERS") if plugin else False,
            request_headers=plugin._request_headers() if plugin else {},
            timeout=int(request.data.get("timeout") or (plugin.get_setting("TIMEOUT_SECONDS") if plugin else 15) or 15),
            verify_ssl=bool(request.data.get("verify_ssl", True)),
        )

        resolved_items = []
        for item in items:
            if isinstance(item, str):
                resolved_items.append({"sku": item})
            elif isinstance(item, dict):
                resolved_items.append(item)
            else:
                resolved_items.append({"sku": str(item)})

        category_override = request.data.get("category_path")
        supplier = request.data.get("supplier") or (plugin._get_supplier() if plugin else None)
        stock_location = plugin._stock_location(request.user) if plugin else None
        image_headers = plugin._request_headers() if plugin else {}

        results = []
        for item in resolved_items:
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
                )
                results.append({
                    "sku": import_result["sku"],
                    "status": "created" if import_result["result"] == "created" else "updated",
                    "part_pk": import_result["part"].pk,
                    "stock_item": import_result["stock_item"].pk if import_result["stock_item"] else None,
                })
            except Exception as exc:  # pragma: no cover - exercised via API tests
                results.append({"sku": sku_hint, "status": "failed", "error": str(exc)})

        return Response({
            "results": results,
            "summary": {
                "total": len(results),
                "created": sum(1 for r in results if r.get("status") == "created"),
                "updated": sum(1 for r in results if r.get("status") == "updated"),
                "failed": sum(1 for r in results if r.get("status") == "failed"),
            },
        }, status=status.HTTP_200_OK)
