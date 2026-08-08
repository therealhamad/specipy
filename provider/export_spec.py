"""Dump the provider's OpenAPI document to stdout.

Used by CI to capture the spec of whatever provider code was just pushed,
without needing the server to be running:

    PROVIDER_VERSION=v2 python -m provider.export_spec > /tmp/openapi.json
"""

from __future__ import annotations

import json
import sys

from provider.app import app


def main() -> int:
    json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
