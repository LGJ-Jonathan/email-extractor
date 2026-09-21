"""Manage API keys.

    python -m app.users add "Sam" [--admin]   prints the new key once
    python -m app.users add portal --service  a key that acts for X-Acting-User
    python -m app.users list
    python -m app.users revoke "Sam"
"""

import argparse
import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.auth import hash_key, new_key
from app.db import dispose_engine, get_sessionmaker
from app.models import User


async def _add(name: str, admin: bool, service: bool = False) -> None:
    key = new_key()
    async with get_sessionmaker()() as s:
        if await s.scalar(select(User).where(User.name == name)):
            raise SystemExit(f"a user named {name!r} already exists")
        s.add(User(id=uuid.uuid4(), name=name, key_hash=hash_key(key), is_admin=admin,
                   is_service=service))
        await s.commit()
    kind = " (service)" if service else " (admin)" if admin else ""
    print(f"created {name}{kind}")
    print(f"key: {key}")
    print("This is the only time the key is shown.")


async def _list() -> None:
    async with get_sessionmaker()() as s:
        for u in await s.scalars(select(User).order_by(User.created_at)):
            state = f"revoked {u.revoked_at:%Y-%m-%d}" if u.revoked_at else "active"
            kind = "service" if u.is_service else "admin" if u.is_admin else "user"
            print(f"{u.name:<32} {kind:<8} {state}")


async def _revoke(name: str) -> None:
    async with get_sessionmaker()() as s:
        u = await s.scalar(select(User).where(User.name == name))
        if u is None:
            raise SystemExit(f"no user named {name!r}")
        u.revoked_at = datetime.now(UTC)
        await s.commit()
    print(f"revoked {name}")


async def _main(args: argparse.Namespace) -> None:
    try:
        if args.cmd == "add":
            await _add(args.name, args.admin, args.service)
        elif args.cmd == "list":
            await _list()
        else:
            await _revoke(args.name)
    finally:
        await dispose_engine()


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.users")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--admin", action="store_true")
    a.add_argument("--service", action="store_true",
                   help="acts for the person in X-Acting-User (the portal)")
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    r.add_argument("name")
    asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    main()
