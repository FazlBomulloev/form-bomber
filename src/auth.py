import os
import hashlib
import hmac
import json
import time
import base64
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

AUTH_LOGIN = os.getenv("AUTH_LOGIN", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "admin")
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-to-random-secret-key-in-production")
JWT_EXP_SECONDS = 60 * 60 * 24 * 7  # 7 days


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _sign(payload: str) -> str:
    return _b64url_encode(
        hmac.new(JWT_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    )


def create_token(login: str) -> str:
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url_encode(json.dumps({
        "sub": login,
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_EXP_SECONDS,
    }).encode())
    unsigned = f"{header}.{payload}"
    signature = _sign(unsigned)
    return f"{unsigned}.{signature}"


def verify_token(token: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        unsigned = f"{parts[0]}.{parts[1]}"
        expected_sig = _sign(unsigned)
        if not hmac.compare_digest(expected_sig, parts[2]):
            return None
        payload = json.loads(_b64url_decode(parts[1]))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def check_credentials(login: str, password: str) -> bool:
    return login == AUTH_LOGIN and password == AUTH_PASSWORD
