"""Shared helpers for ingest/ modules."""
from __future__ import annotations

import os

# Some networks run a TLS-intercepting proxy/content filter (see
# docs/DATA_RECON.md's "Open items" section, README.md). Any ingest module
# making outbound HTTPS calls should verify against the combined CA bundle
# when present, falling back to the default certifi trust store otherwise.
CA_BUNDLE_ENV = "BTS_CA_BUNDLE"


def ca_bundle_verify() -> "bool | str":
    path = os.environ.get(CA_BUNDLE_ENV)
    return path if path else True
