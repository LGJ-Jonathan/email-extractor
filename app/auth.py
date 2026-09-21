"""Per-person API keys (users table) plus the bootstrap admin key (API_KEY)."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from fastapi import Header, HTTPException, status
from sqlalchemy import select

from app.settings import settings

KEY_PREFIX = "ee_"


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID | None      # None for the bootstrap key
    name: str
    is_admin: bool


ADMIN = Principal(user_id=None, name="admin", is_admin=True)


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")


async def require_api_key(x_api_key: str | None = Header(default=None)) -> Principal:
    if not x_api_key:
        raise _unauthorized()
    # compare_digest raises TypeError on non-ASCII str; Starlette decodes headers as
    # latin-1, so any byte >= 0x80 would otherwise surface as an unauthenticated 500.
    # No issued key is non-ASCII, so such a header is rejected before the database.
    if secrets.compare_digest(x_api_key.encode("utf-8"), settings.api_key.encode("utf-8")):
        return ADMIN
    if not x_api_key.isascii() or not x_api_key.startswith(KEY_PREFIX):
        raise _unauthorized()

    from app.db import get_sessionmaker
    from app.models import User

    async with get_sessionmaker()() as session:
        user = await session.scalar(
            select(User).where(User.key_hash == hash_key(x_api_key), User.revoked_at.is_(None))
        )
    if user is None:
        raise _unauthorized()
    return Principal(user_id=user.id, name=user.name, is_admin=user.is_admin)
