"""Installation encryption only. Signing/digest keys are deliberately unrelated."""
import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, MultiFernet

from .config import get_settings


def fingerprint(key=None):
    key = key or get_settings().gateway_master_key
    return hashlib.sha256(b"thunderbolt/master-key/v1\x00" + base64.urlsafe_b64decode(key)).hexdigest()


def primary():
    return Fernet(get_settings().gateway_master_key.encode())


def cipher():
    import json
    settings = get_settings()
    return MultiFernet([Fernet(k.encode()) for k in [settings.gateway_master_key, *json.loads(settings.gateway_master_decrypt_keys)]])


def matches(value):
    return isinstance(value, str) and hmac.compare_digest(value, fingerprint())


def validate_plaintext(value, *, mfa=False):
    decoded = value.decode("utf-8")
    if mfa and len(base64.b32decode(decoded)) != 20:
        raise ValueError("Invalid encrypted seed")
    return decoded
