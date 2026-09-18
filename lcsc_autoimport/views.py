from __future__ import annotations

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .adapter import build_category_path, normalize_lcsc_payload
from .client import LCSCClient
from .service import bulk_import_lcsc_items, import_lcsc_product


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

        remote_client = LCSCClient(
            base_url=request.data.get("api_url") or None,
            api_key=request.data.get("api_key") or None,
            timeout=int(request.data.get("timeout") or 15),
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
        results = []
        for item in resolved_items:
            try:
                if "sku" in item and not item.get("attributes"):
                    product = remote_client.fetch_product(item["sku"])
                    product.update({"category": item.get("category") or product.get("category")})
                else:
                    product = normalize_lcsc_payload(item)
                import_result = import_lcsc_product(product, supplier=request.data.get("supplier") or None, category_path=category_override)
                results.append({
                    "sku": import_result["sku"],
                    "status": "created" if import_result["result"] == "created" else "updated",
                    "part_pk": import_result["part"].pk,
                })
            except Exception as exc:  # pragma: no cover - exercised via API tests
                results.append({"sku": str(item.get("sku") or item.get("productCode") or ""), "status": "failed", "error": str(exc)})

        return Response({
            "results": results,
            "summary": {
                "total": len(results),
                "created": sum(1 for r in results if r.get("status") == "created"),
                "updated": sum(1 for r in results if r.get("status") == "updated"),
                "failed": sum(1 for r in results if r.get("status") == "failed"),
            },
        }, status=status.HTTP_200_OK)
