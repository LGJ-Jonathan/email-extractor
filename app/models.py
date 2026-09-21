"""SQLAlchemy tables (spec section 4)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JOB_STATUSES = ("queued", "running", "paused", "done", "failed")
DOMAIN_STAGES = (
    "pending",
    "dns",
    "discovery",
    "fetching",
    "extracting",
    "judging",
    "done",
)

job_status_enum = Enum(*JOB_STATUSES, name="job_status")
domain_stage_enum = Enum(*DOMAIN_STAGES, name="domain_stage")


class Base(DeclarativeBase):
    pass


class User(Base):
    """One per person. Only a sha256 of the key is stored; the key is shown once."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        job_status_enum, nullable=False, default="queued", server_default="queued"
    )
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    done_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Upload provenance (spec section 14). `columns` preserves original header order so
    # results.csv can replay every source column ahead of the ex_* result columns.
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    columns: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    website_column: Mapped[str | None] = mapped_column(Text, nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Null for jobs created with the bootstrap API_KEY.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    fresh: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    __table_args__ = (Index("ix_jobs_owner_id_created_at", "owner_id", "created_at"),)


class JobDomain(Base):
    """The work queue: one row per unique domain per job (app/queue.py).

    state is queued -> running -> done. Two jobs sharing a domain each get a row, but
    only one fetch runs: the other waits on the domain lock and then reads the cache.
    """

    __tablename__ = "job_domains"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="queued",
                                       server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    from_cache: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Tokens THIS job paid for; 0 when it read someone else's result.
    jina_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index("ix_job_domains_job_state_seq", "job_id", "state", "seq"),
        Index("ix_job_domains_running", "claimed_by", postgresql_where=text("state = 'running'")),
    )


class JobItem(Base):
    """One row per input row, in submission order."""

    __tablename__ = "job_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    input_value: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Row-level outcome for inputs that never reach a domain task (spec section 15):
    # invalid_input plus a reason such as empty / not_a_url / social_or_directory /
    # free_email_only. Null means "defer to the domains row".
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain_source: Mapped[str | None] = mapped_column(Text, nullable=True)  # website | email
    raw_row: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (Index("ix_job_items_job_id_row_index", "job_id", "row_index"),)


class Domain(Base):
    """One row per unique root domain. Doubles as the cache and the checkpoint."""

    __tablename__ = "domains"

    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    stage: Mapped[str] = mapped_column(
        domain_stage_enum, nullable=False, default="pending", server_default="pending"
    )
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Held by the worker fetching this domain, so two jobs never fetch it at once.
    lock_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_domains_finished_at", "finished_at"),)


class Page(Base):
    """Debug and audit trail. Raw HTML is never stored here; keep 30 days."""

    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(
        Text, ForeignKey("domains.domain", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    jina_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_pages_domain_fetched_at", "domain", "fetched_at"),)
