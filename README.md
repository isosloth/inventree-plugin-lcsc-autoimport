# LCSCAutoImport

An InvenTree plugin that imports LCSC products from QR codes, product JSON, and
CSV order exports.

## Installation

### InvenTree Plugin Manager

Install the package in the InvenTree environment, then enable **LCSC Auto
Import** from the plugin settings.

### Command Line 

To install manually via the command line, run the following command:

```bash
pip install inventree-lcsc-auto-import
```

## Configuration

The plugin uses the configured LCSC-compatible product endpoint to fetch
metadata. Set the supplier, category root, and optional stock location in the
plugin settings. API keys and custom request headers are supported.

## Usage

The plugin adds authenticated endpoints for bulk and CSV imports:

- `POST /api/plugin/lcsc-autoimport/bulk/`
- `POST /api/plugin/lcsc-autoimport/csv/`

QR payloads containing an `pc:<SKU>` field are handled by InvenTree's barcode
scanner integration. A `qty:<amount>` field adds stock at the configured
location.
