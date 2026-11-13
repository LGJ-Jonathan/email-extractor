"""Jina and TypeSafe keys set from the portal (admins), encrypted at rest.

A provider can hold several keys (the Jina pool). Requests rotate across the usable
ones; a key the provider rejects (401) or that has run out of credit (402) is benched
for a while and the others carry on, so one empty key no longer pauses every job.

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
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import settings

log = logging.getLogger("email_extractor")

PROVIDERS = ("jina", "typesafe")
REFRESH_S = 30



@dataclass(frozen=True)
class Entry:
    id: str
    key: str
    last4: str
    updated_by: str | None
    updated_at: str | None


_keys: dict[str, list[Entry]] = {}  # provider -> keys saved in the database, oldest first
# Providers with a saved key this process could not decrypt (wrong or missing master
# key, tampered row). They get NO key -- never the environment's. Falling back let a
# worker keep crawling with a key an admin had just replaced because it was leaked.
_failed: set[str] = set()
# key -> (monotonic time it may be used again, why). Per process: the worker makes
# nearly every provider call, and reports its benched keys in process_status.
_benched: dict[str, tuple[float, str]] = {}
_next: dict[str, int] = {}          # provider -> round-robin cursor
_loaded_at = 0.0
_lock = asyncio.Lock()

BENCH_S = {"rejected": 3600.0, "empty": 900.0}


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


def _not_loaded_yet() -> bool:
    """A service process (api, worker) that has not yet read the saved keys does not
    know whether an admin replaced the environment's key, so it has NO key until it
    does. Otherwise a failed first read (a database blip at boot, code started ahead
    of its migration) left it crawling on the environment key indefinitely -- the
    very key that gets replaced from the portal when it leaks. Scripts and tests
    never name themselves and keep using the environment directly."""
    return _process_name is not None and _loaded_at == 0.0


def _env_key(provider: str) -> str:
    return settings.jina_api_key if provider == "jina" else settings.typesafe_api_key


def _is_benched(key: str) -> bool:
    b = _benched.get(key)
    if b is None:
        return False
    if time.monotonic() >= b[0]:
        del _benched[key]
        return False
    return True


def usable(provider: str) -> list[Entry]:
    """Saved keys that are not benched."""
    return [e for e in _keys.get(provider, []) if not _is_benched(e.key)]


def usable_count(provider: str) -> int:
    """How many keys can take requests now (at least 1: the rate limit never drops to
    zero, and the last key standing is still tried so its error can surface)."""
    if provider in _failed:
        return 1
    return max(1, len(usable(provider)))


def current(provider: str) -> str:
    """The key to use for the next request: the usable portal-set keys in rotation,
    else the environment's. Empty when a saved key exists but cannot be decrypted here
    (fail closed). When every saved key is benched, the one due back soonest is
    returned, so the provider's answer (still out of credit, or working again) is seen."""
    if provider in _failed or _not_loaded_yet():
        return ""
    saved = _keys.get(provider)
    if not saved:
        return _env_key(provider)
    ok = usable(provider)
    if not ok:
        return min(saved, key=lambda e: _benched.get(e.key, (0.0, ""))[0]).key
    i = _next.get(provider, 0) % len(ok)
    _next[provider] = i + 1
    return ok[i].key


def bench(provider: str, key: str, reason: str) -> bool:
    """Take a key out of rotation after the provider refused it. True when another
    saved key can carry on, so the caller should retry instead of pausing the job."""
    if not key or not any(e.key == key for e in _keys.get(provider, [])):
        return False                      # the environment's key: nothing to rotate to
    if key not in _benched:
        log.warning("provider_key_benched", extra={"provider": provider, "last4": key[-4:],
                                                   "reason": reason})
    _benched[key] = (time.monotonic() + BENCH_S.get(reason, 900.0), reason)
    return bool(usable(provider))


def unbench(key: str) -> None:
    """The provider accepted it again (credit was added)."""
    _benched.pop(key, None)


def signature(provider: str) -> tuple:
    """Changes when the provider's saved keys change. The worker compares it before
    pausing, to notice a key an admin saved moments ago."""
    # "Not read yet" is part of it: a worker whose first read failed has no key, and
    # must retry once the read succeeds rather than pause every job over a blip.
    return (tuple(e.id for e in _keys.get(provider, [])), provider in _failed,
            _not_loaded_yet())


def source(provider: str) -> str:
    if provider in _failed or _not_loaded_yet():
        return "unreadable"
    if _keys.get(provider):
        return "portal"
    return "environment" if _env_key(provider) else "none"


def describe() -> dict:
    """One summary per provider (the newest saved key, or the environment's)."""
    out = {}
    for p in PROVIDERS:
        saved = _keys.get(p, [])
        newest = saved[-1] if saved else None
        env = _env_key(p)
        out[p] = {
            "source": source(p),
            "decrypt_failed": p in _failed,
            "last4": newest.last4 if newest else (
                _failed_last4.get(p) if p in _failed else (env[-4:] if env else None)),
            "updated_by": newest.updated_by if newest else None,
            "updated_at": newest.updated_at if newest else None,
            "count": len(saved),
        }
    return out


def entries(provider: str) -> list[Entry]:
    return list(_keys.get(provider, []))


def benched_report() -> dict:
    """{key id: reason} for this process's benched keys. Ids only, never keys."""
    out = {}
    for p in PROVIDERS:
        for e in _keys.get(p, []):
            if _is_benched(e.key):
                out[e.id] = _benched[e.key][1]
    return out


_failed_last4: dict[str, str] = {}


async def refresh(session: AsyncSession, *, force: bool = False) -> None:
    """Reload saved keys from the database (at most every REFRESH_S unless forced)."""
    global _loaded_at
    async with _lock:
        if not force and time.monotonic() - _loaded_at < REFRESH_S:
            return
        rows = (await session.execute(text(
            "SELECT id, provider, ciphertext, nonce, last4, updated_by, updated_at "
            "FROM provider_keys ORDER BY updated_at, id"
        ))).all()
        keys: dict[str, list[Entry]] = {}
        failed: set[str] = set()
        failed_last4: dict[str, str] = {}
        for r in rows:
            try:
                secret = decrypt(r.provider, r.ciphertext, r.nonce)
            except KeyStoreUnavailable as e:
                failed.add(r.provider); failed_last4[r.provider] = r.last4
                log.error("provider_key_unavailable", extra={"provider": r.provider,
                                                             "err": str(e)})
                continue
            except Exception:  # noqa: BLE001 - wrong master key or tampered row
                failed.add(r.provider); failed_last4[r.provider] = r.last4
                log.error("provider_key_decrypt_failed", extra={"provider": r.provider})
                continue
            keys.setdefault(r.provider, []).append(Entry(
                id=str(r.id), key=secret, last4=r.last4, updated_by=r.updated_by,
                updated_at=r.updated_at.isoformat() if r.updated_at else None))
        _keys.clear(); _keys.update(keys)
        _failed.clear(); _failed.update(failed)
        _failed_last4.clear(); _failed_last4.update(failed_last4)
        live = {e.key for es in keys.values() for e in es}
        for k in [k for k in _benched if k not in live]:
            del _benched[k]
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

    described = describe()
    status = {p: {"source": described[p]["source"], "last4": described[p]["last4"],
                  "count": described[p]["count"]} for p in PROVIDERS}
    status["benched"] = benched_report()
    try:
        await _merge_status(session, status)
    except Exception:  # noqa: BLE001 - reporting must never break key loading
        await session.rollback()
        log.exception("process_status_report_failed")


async def _merge_status(session: AsyncSession, fields: dict) -> None:
    """Merge into this process's row (jsonb ||), so the key report and the worker's
    health report do not overwrite each other."""
    import json

    await session.execute(
        text("""
            INSERT INTO process_status (name, status, updated_at)
            VALUES (:n, CAST(:s AS jsonb), now())
            ON CONFLICT (name) DO UPDATE
            SET status = process_status.status || CAST(:s AS jsonb), updated_at = now()
        """),
        {"n": _process_name, "s": json.dumps(fields)},
    )
    await session.commit()


async def report_health(session: AsyncSession, health: dict) -> None:
    """The worker's liveness, written on every heartbeat under "health"."""
    if _process_name:
        await _merge_status(session, {"health": health})


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


async def _insert(session: AsyncSession, provider: str, secret: str, who: str) -> None:
    ciphertext, nonce = encrypt(provider, secret)
    await session.execute(
        text("""
            INSERT INTO provider_keys (provider, ciphertext, nonce, last4, updated_by, updated_at)
            VALUES (:p, :c, :n, :l, :w, now())
        """),
        {"p": provider, "c": ciphertext, "n": nonce, "l": secret[-4:], "w": who},
    )


async def save(session: AsyncSession, provider: str, secret: str, who: str,
               request_id: str | None = None) -> None:
    """Make this the provider's ONLY key (replaces whatever was saved)."""
    encrypt(provider, secret)             # fail before deleting if the store is unusable
    await session.execute(text("DELETE FROM provider_keys WHERE provider = :p"), {"p": provider})
    await _insert(session, provider, secret, who)
    await audit(session, provider, "saved", secret[-4:], who, request_id)
    await session.commit()
    await refresh(session, force=True)


class PoolFull(RuntimeError):
    pass


async def add(session: AsyncSession, provider: str, secret: str, who: str,
              request_id: str | None = None, max_keys: int | None = None) -> bool:
    """Add a key to the provider's pool. False when it is already there.

    The check and the insert run under one advisory lock per provider, and read the
    table itself rather than this process's copy: two POSTs of the same key at once
    (a double click, two API replicas) both passed a check-then-insert, and the key
    then sat in the rotation twice, counted as two keys' worth of rate limit."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                          {"k": f"provider_keys:{provider}"})
    rows = (await session.execute(
        text("SELECT ciphertext, nonce FROM provider_keys WHERE provider = :p"),
        {"p": provider},
    )).all()
    for r in rows:
        try:
            if decrypt(provider, r.ciphertext, r.nonce) == secret:
                await session.rollback()
                return False
        except KeyStoreUnavailable:
            await session.rollback()
            raise
        except Exception as e:  # noqa: BLE001 - wrong master key or tampered row
            # Cannot compare against a key we cannot read; adding beside it would
            # leave the provider failed-closed anyway. Replacing (PUT) is the way out.
            await session.rollback()
            raise KeyStoreUnavailable(
                "a saved key cannot be decrypted; replace it instead") from e
    if max_keys is not None and len(rows) >= max_keys:
        await session.rollback()
        raise PoolFull(provider)
    await _insert(session, provider, secret, who)
    await audit(session, provider, "added", secret[-4:], who, request_id)
    await session.commit()
    await refresh(session, force=True)
    return True


async def remove(session: AsyncSession, provider: str, key_id: str, who: str,
                 request_id: str | None = None) -> bool:
    row = (await session.execute(
        text("DELETE FROM provider_keys WHERE provider = :p AND id = CAST(:i AS uuid) "
             "RETURNING last4"),
        {"p": provider, "i": key_id},
    )).first()
    if row is None:
        await session.rollback()
        return False
    await audit(session, provider, "removed", row.last4, who, request_id)
    await session.commit()
    await refresh(session, force=True)
    return True


async def clear(session: AsyncSession, provider: str, who: str,
                request_id: str | None = None) -> None:
    await session.execute(text("DELETE FROM provider_keys WHERE provider = :p"), {"p": provider})
    await audit(session, provider, "removed", None, who, request_id)
    await session.commit()
    await refresh(session, force=True)


def reset_for_tests() -> None:
    global _loaded_at, _process_name
    _keys.clear(); _failed.clear(); _failed_last4.clear(); _benched.clear(); _next.clear()
    _loaded_at = 0.0; _process_name = None
