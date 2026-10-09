import hashlib
import hmac
import re
from collections.abc import Sequence
from dataclasses import dataclass

_KEY_RE = re.compile(r"mlapi_([A-Za-z0-9]{12})_([A-Za-z0-9_-]{43})", re.ASCII)


@dataclass(frozen=True)
class ParsedKey:
    key_id: str
    secret: str


def parse_key(raw: str | None) -> ParsedKey | None:
    if not raw:
        return None
    match = _KEY_RE.fullmatch(raw)
    if match is None:
        return None
    return ParsedKey(key_id=match.group(1), secret=match.group(2))


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def verify_secret(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret).encode(), (stored_hash or "").encode())


def model_allowed(model: str, allowed_models: Sequence[str], allow_all: bool) -> bool:
    if allow_all:
        return True
    for pattern in allowed_models:
        if pattern == "*":
            continue
        if pattern.endswith("*"):
            if model.startswith(pattern[:-1]):
                return True
        elif model == pattern:
            return True
    return False


def is_expired(expires_at: float | int | None, now: float) -> bool:
    return expires_at is not None and now >= expires_at


@dataclass(frozen=True)
class AuthContext:
    key_id: str
    allowed_models: tuple[str, ...]
    allow_all: bool

    def can_use(self, model: str) -> bool:
        return model_allowed(model, self.allowed_models, self.allow_all)
