"""Jina and TypeSafe keys set from the portal (admins), encrypted at rest.

A key saved here overrides the JINA_API_KEY / TYPESAFE_API_KEY environment variables
without a redeploy: every process refreshes its copy every REFRESH_S. Keys are
AES-GCM encrypted under PROVIDER_KEY_ENCRYPTION_KEY (32 bytes, base64), which lives
only in the service's environment. Only the last four characters ever leave.
"""

import asyncio
import base64
import logging
import os
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import settings

log = logging.getLogger("email_extractor")

PROVIDERS = ("jina", "typesafe")
REFRESH_S = 30

_keys: dict[str, str] = {}          # provider -> key saved in the database
_meta: dict[str, dict] = {}         # provider -> {last4, updated_by, updated_at}
# Providers with a saved key this process could not decrypt (wrong or missing master
# key, tampered row). They get NO key -- never the environment's. Falling back let a
# worker keep crawling with a key an admin had just replaced because it was leaked.
_failed: set[str] = set()
_loaded_at = 0.0
_lock = asyncio.Lock()


class KeyStoreUnavailable(RuntimeError):
    pass


def _aead():
    raw = settings.provider_key_encryption_key
    if not raw:
        raise KeyStoreUnavailable("PROVIDER_KEY_ENCRYPTION_KEY is not set")
    try:
        key = base64.b64decode(raw)
    except ValueError as e:
        raise KeyStoreUnavailable("PROVIDER_KEY_ENCRYPTION_KEY is not base64") from e
    if len(key) != 32:
        raise KeyStoreUnavailable("PROVIDER_KEY_ENCRYPTION_KEY must decode to 32 bytes")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key)


def encrypt(provider: str, secret: str) -> tuple[str, str]:
    nonce = os.urandom(12)
    # The provider name is bound in as associated data, so a Jina ciphertext copied
    # into the TypeSafe row does not decrypt.
    ct = _aead().encrypt(nonce, secret.encode(), provider.encode())
    return base64.b64encode(ct).decode(), base64.b64encode(nonce).decode()


def decrypt(provider: str, ciphertext: str, nonce: str) -> str:
    return _aead().decrypt(base64.b64decode(nonce), base64.b64decode(ciphertext),
                           provider.encode()).decode()


def current(provider: str) -> str:
    """The key to use now: the portal-set one if there is one, else the environment's.
    Empty when a saved key exists but cannot be decrypted here (fail closed)."""
    if provider in _failed:
        return ""
    if provider in _keys:
        return _keys[provider]
    return settings.jina_api_key if provider == "jina" else settings.typesafe_api_key


def source(provider: str) -> str:
    if provider in _failed:
        return "unreadable"
    if provider in _keys:
        return "portal"
    return "environment" if current(provider) else "none"


def describe() -> dict:
    out = {}
    for p in PROVIDERS:
        key = current(p)
        meta = _meta.get(p, {})
        out[p] = {
            "source": source(p),
            "decrypt_failed": p in _failed,
            "last4": meta.get("last4") if (p in _keys or p in _failed) else (key[-4:] if key else None),
            "updated_by": meta.get("updated_by"),
            "updated_at": meta.get("updated_at"),
        }
    return out


async def refresh(session: AsyncSession, *, force: bool = False) -> None:
    """Reload saved keys from the database (at most every REFRESH_S unless forced)."""
    global _loaded_at
    async with _lock:
        if not force and time.monotonic() - _loaded_at < REFRESH_S:
            return
        rows = (await session.execute(text(
            "SELECT provider, ciphertext, nonce, last4, updated_by, updated_at FROM provider_keys"
        ))).all()
        keys, meta, failed = {}, {}, set()
        for r in rows:
            meta[r.provider] = {"last4": r.last4, "updated_by": r.updated_by,
                                "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            try:
                keys[r.provider] = decrypt(r.provider, r.ciphertext, r.nonce)
            except KeyStoreUnavailable as e:
                failed.add(r.provider)
                log.error("provider_key_unavailable", extra={"provider": r.provider,
                                                             "err": str(e)})
            except Exception:  # noqa: BLE001 - wrong master key or tampered row
                failed.add(r.provider)
                log.error("provider_key_decrypt_failed", extra={"provider": r.provider})
        _keys.clear(); _keys.update(keys)
        _meta.clear(); _meta.update(meta)
        _failed.clear(); _failed.update(failed)
        _loaded_at = time.monotonic()
        if _process_name:
            await _report(session)


_process_name: str | None = None


def set_process_name(name: str) -> None:
    """'api' or 'worker': each writes what it is using to process_status on refresh."""
    global _process_name
    _process_name = name


async def _report(session: AsyncSession) -> None:
    import json

    status = {p: {"source": source(p), "last4": (current(p) or "")[-4:] or None}
              for p in PROVIDERS}
    try:
        await session.execute(
            text("""
                INSERT INTO process_status (name, status, updated_at)
                VALUES (:n, CAST(:s AS jsonb), now())
                ON CONFLICT (name) DO UPDATE SET status = CAST(:s AS jsonb), updated_at = now()
            """),
            {"n": _process_name, "s": json.dumps(status)},
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - reporting must never break key loading
        await session.rollback()
        log.exception("process_status_report_failed")


async def processes(session: AsyncSession) -> dict:
    rows = await session.execute(text("SELECT name, status, updated_at FROM process_status"))
    return {r.name: {**r.status, "updated_at": r.updated_at.isoformat()} for r in rows}


async def audit(session: AsyncSession, provider: str, action: str, last4: str | None,
                actor: str, request_id: str | None) -> None:
    await session.execute(
        text("""
            INSERT INTO provider_key_audit (provider, action, last4, actor, request_id)
            VALUES (:p, :a, :l, :w, :r)
        """),
        {"p": provider, "a": action, "l": last4, "w": actor, "r": request_id},
    )


async def recent_audit(session: AsyncSession, limit: int = 10) -> list[dict]:
    rows = await session.execute(
        text("SELECT provider, action, last4, actor, at FROM provider_key_audit "
             "ORDER BY at DESC LIMIT :n"),
        {"n": limit},
    )
    return [{"provider": r.provider, "action": r.action, "last4": r.last4, "actor": r.actor,
             "at": r.at.isoformat()} for r in rows]


async def save(session: AsyncSession, provider: str, secret: str, who: str,
               request_id: str | None = None) -> None:
    ciphertext, nonce = encrypt(provider, secret)
    await session.execute(
        text("""
            INSERT INTO provider_keys (provider, ciphertext, nonce, last4, updated_by, updated_at)
            VALUES (:p, :c, :n, :l, :w, now())
            ON CONFLICT (provider) DO UPDATE SET ciphertext = :c, nonce = :n, last4 = :l,
                updated_by = :w, updated_at = now()
        """),
        {"p": provider, "c": ciphertext, "n": nonce, "l": secret[-4:], "w": who},
    )
    await audit(session, provider, "saved", secret[-4:], who, request_id)
    await session.commit()
    await refresh(session, force=True)


async def clear(session: AsyncSession, provider: str, who: str,
                request_id: str | None = None) -> None:
    await session.execute(text("DELETE FROM provider_keys WHERE provider = :p"), {"p": provider})
    await audit(session, provider, "removed", None, who, request_id)
    await session.commit()
    await refresh(session, force=True)


def reset_for_tests() -> None:
    global _loaded_at, _process_name
    _keys.clear(); _meta.clear(); _failed.clear(); _loaded_at = 0.0; _process_name = None
