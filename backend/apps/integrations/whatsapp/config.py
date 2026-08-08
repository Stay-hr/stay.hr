from __future__ import annotations

import os


def webhook_verify_token_from_env() -> str:
    return os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "").strip()


def app_secret_from_env() -> str:
    return os.getenv("WHATSAPP_APP_SECRET", "").strip()


def api_version_from_env() -> str:
    return os.getenv("WHATSAPP_API_VERSION", "v23.0").strip() or "v23.0"


def webhook_verify_signature_from_env() -> bool:
    raw = os.getenv("WHATSAPP_WEBHOOK_VERIFY_SIGNATURE", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def meta_app_id_from_env() -> str:
    return os.getenv("META_APP_ID", "").strip()


def waba_id_from_env() -> str:
    return os.getenv("WHATSAPP_WABA_ID", "").strip()


def access_token_from_env() -> str:
    return os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
