# InvenTree LCSC Auto Import

An installable InvenTree plugin for automatically importing LCSC parts into your inventory from order lists and QR/barcode data.

## What this plugin does

- Accepts an LCSC product code (for example `C312270`) and fetches the relevant product JSON from a configurable endpoint.
- Creates or updates the matching InvenTree `Part` and `SupplierPart` records.
- Maps the LCSC product category to an InvenTree `PartCategory` tree using a configuration map.
- Creates or reuses `ParameterTemplate` objects and stores product attributes as `Part` parameters.
- Handles LCSC QR payloads such as:
  `{"pbn":"...","on":"...","pc":"C312270",...}`
- Supports bulk import of a list of LCSC part codes via a plugin endpoint.

## Installation

Install the package into the same Python environment that runs InvenTree:

```bash
pip install inventree-plugin-lcsc-autoimport
```

If you are developing from this repository:

```bash
git clone https://github.com/isosloth/inventree-plugin-lcsc-autoimport.git
cd inventree-plugin-lcsc-autoimport
pip install -e .
```

Then enable the plugin inside the InvenTree admin / plugin settings panel.

## Required configuration

After installation, open the plugin settings and configure:

- `LCSC Supplier` — the InvenTree supplier company record that represents LCSC
- `LCSC API URL` — the endpoint used to fetch product JSON
- `LCSC API Key` — optional bearer token / API key if your endpoint requires it
- `Send Authentication Headers` — disabled by default; enable it only when the configured endpoint requires `Authorization` and `X-API-Key`
- `Request Headers` — a JSON object of additional headers to send on every request; for example, `{"User-Agent": "Example client", "Origin": "https://www.lcsc.com", "Referer": "https://www.lcsc.com/"}`
- `Category Root Path` — every imported API category is created beneath this path; for example, `Electronics/PCB-Parts`
- `Default Category Path` — fallback category name relative to the category root, such as `Uncategorized`
- `Category Mapping` — optional JSON mapping of remote category names to paths relative to the category root; for example, `{"Resistors": "Passives/Resistors"}` becomes `Electronics/PCB-Parts/Passives/Resistors`
- `Default Stock Location` — fallback location used when scanning an LCSC QR code with `qty` or `quantity` and the scanning user has not set their own location; existing stock at the resolved location is incremented
- Each user can also set their own **Default Stock Location** from their account plugin settings page (`/settings/user/plugin-settings/`); a user's own value always takes priority over the admin-configured default above
- `Fetch Enabled` — whether remote fetches are enabled
- `Price Currency Symbol` — only LCSC price-break entries reported under this currency symbol are imported (default `€`); LCSC reports the same quantity ladder in several currencies, so this selects which one becomes the `SupplierPart` price breaks
- `Price Currency Code` — ISO currency code to store the imported price breaks under; leave blank to auto-detect from the symbol (`€` → `EUR`, `$` → `USD`, `£` → `GBP`, `¥` → `CNY`)

A good default URL is the LCSC-compatible endpoint you use in your environment; the code is intentionally written so you can swap the remote adapter without changing the rest of the plugin.

## Pricing

When the LCSC payload includes a `productPriceList` array (quantity price breaks per currency), the plugin imports the
ladder matching `Price Currency Symbol` as `SupplierPriceBreak` records on the `SupplierPart` — one row per reported
quantity break (`ladder`) and unit price (`currencyPrice`). Re-importing a SKU updates existing breaks in place rather
than duplicating them.

## Bulk import endpoint

The plugin exposes an authenticated JSON endpoint:

```http
POST /api/plugin/lcsc-autoimport/bulk/
```

Example payload:

```json
{
  "items": [
    {"sku": "C312270", "quantity": 2},
    {"sku": "C0402C104K5RACTU", "quantity": 5},
    {"sku": "C345678"}
  ]
}
```

You may also submit:

```json
{"skus": ["C312270", "C0402C104K5RACTU"]}
```

The endpoint returns a summary including created, updated, skipped, and failed items.

## CSV order import

To import an entire LCSC order or BOM without typing SKUs by hand, upload a CSV export:

```http
POST /api/plugin/lcsc-autoimport/csv/
```

Either upload a file (multipart form field `file`) or POST raw CSV text as JSON:

```json
{"csv": "LCSC Part Number,Order Qty.\nC312270,25\nC0402C104K5RACTU,100\n"}
```

Column names are matched case-insensitively and tolerate punctuation, so common LCSC order-history export headers
such as `LCSC Part Number` / `Order Qty.` are recognized automatically, alongside simpler `SKU` / `Quantity` headers.
Rows are imported the same way as the bulk JSON endpoint — including stock quantity, pricing, images, and the full
category chain — and a summary of created/updated/failed rows is returned. The file must be UTF-8 encoded and under
2 MiB.

## QR scanning behavior

When a scanned LCSC QR payload is sent to the plugin, it extracts the SKU from the `pc` field and reuses the same import logic as the bulk endpoint. If the SKU is already known, the plugin resolves the existing supplier part. If it is not known, it imports it first and then returns the matched InvenTree part.

This means the scan flow is idempotent and does not create duplicate part records.

## How category and parameter mapping works

The plugin keeps a single normalization layer in `lcsc_autoimport.adapter` so it can adapt to different LCSC-compatible response formats. The remote payload is normalized to a common shape before it is imported.

The plugin:

1. converts the remote category name to a stable internal value
2. resolves the target InvenTree category path
3. creates the category tree if needed
4. creates or reuses the matching `ParameterTemplate` objects
5. stores the normalized parameter values on the `Part`

When the LCSC payload includes a `parentCatalogList` array and a `parentCatalogName`, the plugin builds the full nested category chain (each `catalogNameEn` entry, in order, followed by `parentCatalogName` as the final leaf category) beneath `Category Root Path` — for example `Electronics/PCB-Parts/Passives/Capacitors/Aluminum Electrolytic Capacitors`. `Category Mapping` is checked against the final (deepest) category name and, if matched, replaces the whole chain with the mapped path. Missing or unknown categories fall back to `Default Category Path` below that root.

Each `catalogId` (from `parentCatalogList[].catalogId`) and the leaf category's `wmCatalogId` are stashed in the created category's metadata as `lcsc_catalog_id`, for traceability back to LCSC's own category ids. This only happens for newly created categories built from the automatic chain (not when a `Category Mapping` override applies).

## Parameters

Each imported attribute is saved as an InvenTree `ParameterTemplate`. The template's internal `name` is set to LCSC's own stable `paramId` (falling back to a slug derived from the parameter's display name if no id is present in the payload), so that the same underlying attribute is reused across categories/parts even when LCSC's human-readable label (`paramNameEn`) differs between categories. The human-readable label is kept on the template's `description` field for display purposes.

### Package / mounting type ("encap")

The plugin looks for an encapsulation/package attribute (either a dedicated top-level API field, or any parameter whose name contains "encap" or "package") and splits it into up to two parameters, smarter than LCSC's raw value:

- A raw value such as `SMD,SOD-123` or `Through Hole,TO-92` is split on the comma into a **Mounting Type** parameter (normalized to `SMD` or `THT`) and a **Package / Encapsulation** parameter (the remaining package designator, e.g. `SOD-123`).
- A raw value with no comma, such as `Plugin`, is stored as-is in the **Package / Encapsulation** parameter, with no Mounting Type parameter created.
- If no such field or attribute is found in the payload, no package/mounting parameters are added.

## Security notes

- Keep the API key in the InvenTree plugin settings and do not hard-code secrets.
- Treat remote product metadata as untrusted input and sanitize values before creating parameters.
- The plugin logs warnings and returns structured errors for malformed payloads, HTTP failures, and missing fields.

## Limitations

- LCSC's public product API is not standardized across all endpoints and may vary by provider or third-party integration.
- The plugin is designed to support any LCSC-compatible product endpoint via configuration; it does not assume an official API contract beyond a JSON shape that can be normalized.
- If your environment returns a different field layout, update the remote adapter in `lcsc_autoimport.adapter` rather than changing the rest of the plugin logic.

## Development and testing

```bash
pip install -e .[dev]
pytest -q
```

The repository includes a small set of focused tests around the adapter and service logic, using mocked HTTP responses.

## License

MIT
