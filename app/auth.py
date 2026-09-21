"""Who is calling.

Three kinds of caller:
- the bootstrap admin key (API_KEY), which sees everything;
- a personal key (`python -m app.users add NAME`), which sees its own jobs;
- a service key (`python -m app.users add portal --service`), used by the portal. It
  must name the signed-in person in X-Acting-User, and everything -- job ownership,
  fair sharing, limits, visibility -- then applies to that person, not to the portal.
  X-Acting-Role: admin lets a portal admin see everyone's jobs. Both headers are
  trusted only from a service key; sent with any other key they are refused.
"""

import hashlib
import re
import secrets
import time
import uuid
from dataclasses import dataclass

from fastapi import Header, Request
from sqlalchemy import func, select, text

from app.errors import ApiError
from app.settings import settings

KEY_PREFIX = "ee_"
MIN_ADMIN_KEY_LEN = 32
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[A-Za-z]{2,24}$")

# Status polling is exempt from the rate limit: the portal polls every few seconds per
# open job, and counting that would lock people out for watching their own progress.
_UNLIMITED = re.compile(r"^/jobs(/[^/]+(/recent)?)?$")


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID | None      # None for the bootstrap key
    name: str
    is_admin: bool
    via: str | None = None         # the service key's name when acting for someone


ADMIN = Principal(user_id=None, name="admin", is_admin=True)


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def admin_key_usable() -> bool:
    """The bootstrap key only works when it is not a placeholder anyone could guess."""
    key = settings.api_key or ""
    return key != "change-me" and len(key) >= MIN_ADMIN_KEY_LEN


def _invalid() -> ApiError:
    return ApiError("invalid_api_key", "the API key is wrong or has been revoked")


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
    x_acting_user: str | None = Header(default=None),
    x_acting_role: str | None = Header(default=None),
) -> Principal:
    if not x_api_key:
        raise ApiError("missing_api_key", "send your API key in the X-API-Key header")
    # compare_digest raises TypeError on non-ASCII str; Starlette decodes headers as
    # latin-1, so any byte >= 0x80 would otherwise surface as an unauthenticated 500.
    # No issued key is non-ASCII, so such a header is rejected before the database.
    if admin_key_usable() and secrets.compare_digest(
        x_api_key.encode("utf-8"), settings.api_key.encode("utf-8")
    ):
        if x_acting_user or x_acting_role:
            raise ApiError("acting_user_not_allowed",
                           "X-Acting-User is only accepted from a service key")
        return ADMIN
    if not x_api_key.isascii() or not x_api_key.startswith(KEY_PREFIX):
        raise _invalid()

    from app.db import get_sessionmaker
    from app.models import User

    async with get_sessionmaker()() as session:
        user = await session.scalar(
            select(User).where(User.key_hash == hash_key(x_api_key), User.revoked_at.is_(None))
        )
        if user is None:
            raise _invalid()
        if not user.is_service:
            if x_acting_user or x_acting_role:
                raise ApiError("acting_user_not_allowed",
                               "X-Acting-User is only accepted from a service key")
            principal = Principal(user_id=user.id, name=user.name, is_admin=user.is_admin)
        else:
            principal = await _acting_principal(session, user.name, x_acting_user,
                                                x_acting_role)
    await _rate_limit(request, principal)
    return principal


async def _acting_principal(session, service_name: str, email: str | None,
                            role: str | None) -> Principal:
    from app.models import User

    if not email:
        raise ApiError("acting_user_required",
                       "a service key must name the person it acts for in X-Acting-User")
    email = email.strip().lower()
    if not _EMAIL.match(email):
        raise ApiError("invalid_acting_user", "X-Acting-User must be an email address")
    # Read first: this runs on every request, polls included, and a write per poll
    # was pure overhead. Matched case-insensitively, so a person added by hand as
    # "Bob@X.com" is the same person the portal names as bob@x.com.
    person = await session.scalar(select(User).where(func.lower(User.name) == email))
    if person is None:
        # Keyless row: the hash is of a random secret nobody holds, so it can never log in.
        await session.execute(
            text("""
                INSERT INTO users (id, name, key_hash, is_admin, is_service)
                VALUES (:id, :name, :h, false, false)
                ON CONFLICT (name) DO NOTHING
            """),
            {"id": uuid.uuid4(), "name": email,
             "h": hash_key("acting:" + secrets.token_hex(32))},
        )
        await session.commit()
        person = await session.scalar(select(User).where(func.lower(User.name) == email))
    if person.revoked_at is not None:
        # 403, not 401: the portal's own key is fine; this person was removed.
        raise ApiError("acting_user_revoked", f"{email} has been removed from the extractor")
    return Principal(user_id=person.id, name=email, is_admin=(role or "").lower() == "admin",
                     via=service_name)


# --- per-person request rate limit -------------------------------------------

_redis = None


def _client():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(settings.redis_url, socket_timeout=0.5,
                                   socket_connect_timeout=0.5)
    return _redis


async def _rate_limit(request: Request, who: Principal) -> None:
    """A fixed one-minute window per person. Fails open: a Redis outage must not lock
    the whole team out of results they are waiting for."""
    limit = settings.rate_limit_per_minute
    if not limit or who.is_admin and who.via is None:
        return
    if request.method == "GET" and _UNLIMITED.match(request.url.path):
        return
    window = int(time.time() // 60)
    key = f"rl:{who.user_id}:{window}"
    try:
        r = _client()
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, 70)
    except Exception:  # noqa: BLE001
        return
    if count > limit:
        retry = 60 - int(time.time() % 60) or 1
        raise ApiError("rate_limited",
                       f"too many requests: the limit is {limit} a minute per person",
                       retry_after=retry, details={"limit": limit, "window_s": 60})
