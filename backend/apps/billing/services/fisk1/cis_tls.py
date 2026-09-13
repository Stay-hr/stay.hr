from __future__ import annotations

from pathlib import Path

from apps.billing.exceptions import FiscalizationError

CIS_CA_BUNDLE_PATH = (
    Path(__file__).resolve().parent / "certs" / "fina-cis-ca-bundle.pem"
)


def cis_verify_path() -> str:
    """Path to the official FINA CA bundle used to verify CIS TLS."""
    if not CIS_CA_BUNDLE_PATH.is_file():
        raise FiscalizationError("FINA CIS CA bundle is missing.")
    return str(CIS_CA_BUNDLE_PATH)
