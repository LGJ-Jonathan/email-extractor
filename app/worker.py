"""arq worker (spec section 9). Queues and job functions land in build step 8."""

from arq.connections import RedisSettings

from app.settings import settings


async def noop(ctx: dict) -> None:
    """Placeholder: arq raises "at least one function or cron_job must be registered"
    with an empty `functions` list. Replaced by the real task functions in step 8."""
    return None


async def startup(ctx: dict) -> None:
    pass


async def shutdown(ctx: dict) -> None:
    from app.db import dispose_engine

    await dispose_engine()


class WorkerSettings:
    """Default (fetch) worker. The `judge` queue gets its own settings in step 8."""

    functions: list = [noop]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.global_fetch_concurrency
    job_timeout = 300
    max_tries = 3
    on_startup = startup
    on_shutdown = shutdown
