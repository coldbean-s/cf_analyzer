"""PostgreSQL persistence for CF Analyzer (Web version).

Uses SQLAlchemy 2.x async with asyncpg.
Every query involving user data requires user_id for isolation.
"""

from datetime import datetime, timezone, timedelta

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, Integer, String, Text,
    ForeignKey, Index, UniqueConstraint, func, text, select, update, delete,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship

import config

# ---------------------------------------------------------------------------
# Engine / session
# ---------------------------------------------------------------------------

engine = create_async_engine(config.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Create all tables (dev convenience; production uses Alembic)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_db():
    await engine.dispose()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    github_id = Column(BigInteger, unique=True, nullable=False)
    github_login = Column(String, nullable=False)
    github_avatar_url = Column(String, default="")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_login_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class UserConfig(Base):
    __tablename__ = "user_configs"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    active_llm = Column(String, default="claude")
    claude_api_key_enc = Column(String, default="")
    claude_base_url = Column(String, default="")
    claude_model = Column(String, default="claude-sonnet-4-6")
    deepseek_api_key_enc = Column(String, default="")
    deepseek_model = Column(String, default="deepseek-chat")
    cf_handle = Column(String, default="")
    same_language_only = Column(Boolean, default=True)
    request_delay = Column(Float, default=2.0)
    lgm_handles = Column(JSONB, default=list)
    compare_mode = Column(String, default="auto")
    compare_target = Column(String, default="")
    compare_targets = Column(JSONB, default=list)


class Analysis(Base):
    __tablename__ = "analyses"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    contest_id = Column(Integer, nullable=False)
    problem_index = Column(String, nullable=False)
    user_lang = Column(String, nullable=False, default="")
    user_code = Column(Text, nullable=False, default="")
    lgm_source = Column(Text)
    lgm_url = Column(String)
    lgm_lang = Column(String)
    lgm_sid = Column(Integer)
    lgm_handle = Column(String)
    analysis = Column(Text, default="")
    notes = Column(Text, default="")
    style_review = Column(Text, default="")
    problem_statement = Column(Text, default="")
    importance = Column(String, default="")
    last_reviewed_at = Column(String, default="")
    handle = Column(String, default="")
    user_submission_id = Column(Integer)
    created_at = Column(String, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "contest_id", "problem_index", name="uq_analyses_user_problem"),
        Index("ix_analyses_user_id", "user_id"),
    )


class Submission(Base):
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True, autoincrement=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    handle = Column(String, nullable=False)
    contest_id = Column(Integer, nullable=False)
    problem_index = Column(String, nullable=False)
    problem_name = Column(String, nullable=False, default="")
    rating = Column(Integer)
    tags = Column(String, default="[]")
    verdict = Column(String, nullable=False)
    language = Column(String)
    submitted_at = Column(String, nullable=False)

    __table_args__ = (
        Index("ix_submissions_user_problem", "user_id", "contest_id", "problem_index"),
        Index("ix_submissions_user_verdict", "user_id", "verdict"),
    )


class SyncState(Base):
    __tablename__ = "sync_state"

    handle = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    last_submission_id = Column(Integer, default=0)
    last_sync_at = Column(String)
    cold_start_done = Column(Integer, default=0)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def upsert_user(github_id: int, github_login: str, avatar_url: str = "") -> User:
    async with async_session() as s:
        row = (await s.execute(
            select(User).where(User.github_id == github_id)
        )).scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if row:
            row.github_login = github_login
            row.github_avatar_url = avatar_url
            row.last_login_at = now
        else:
            row = User(
                github_id=github_id,
                github_login=github_login,
                github_avatar_url=avatar_url,
                created_at=now,
                last_login_at=now,
            )
            s.add(row)
            await s.flush()
            # Create default user_configs
            s.add(UserConfig(user_id=row.id))

        await s.commit()
        await s.refresh(row)
        return row


async def get_user_by_id(user_id: int) -> User | None:
    async with async_session() as s:
        return (await s.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()


# ---------------------------------------------------------------------------
# User configs
# ---------------------------------------------------------------------------

async def get_user_config(user_id: int) -> UserConfig | None:
    async with async_session() as s:
        row = (await s.execute(
            select(UserConfig).where(UserConfig.user_id == user_id)
        )).scalar_one_or_none()
        if not row:
            row = UserConfig(user_id=user_id)
            s.add(row)
            await s.commit()
            await s.refresh(row)
        return row


async def update_user_config(user_id: int, **kwargs) -> None:
    async with async_session() as s:
        await s.execute(
            update(UserConfig).where(UserConfig.user_id == user_id).values(**kwargs)
        )
        await s.commit()


# ---------------------------------------------------------------------------
# Analyses CRUD
# ---------------------------------------------------------------------------

async def insert_analysis(record: dict, user_id: int | None = None) -> None:
    async with async_session() as s:
        row = Analysis(user_id=user_id, **record)
        s.add(row)
        await s.commit()


async def update_analysis_text(aid: str, text_: str, user_id: int | None = None) -> None:
    async with async_session() as s:
        q = update(Analysis).where(Analysis.id == aid)
        if user_id is not None:
            q = q.where(Analysis.user_id == user_id)
        await s.execute(q.values(analysis=text_))
        await s.commit()


async def update_notes(aid: str, notes: str, user_id: int | None = None) -> None:
    async with async_session() as s:
        q = update(Analysis).where(Analysis.id == aid)
        if user_id is not None:
            q = q.where(Analysis.user_id == user_id)
        await s.execute(q.values(notes=notes))
        await s.commit()


async def update_style_review(aid: str, text_: str, user_id: int | None = None) -> None:
    async with async_session() as s:
        q = update(Analysis).where(Analysis.id == aid)
        if user_id is not None:
            q = q.where(Analysis.user_id == user_id)
        await s.execute(q.values(style_review=text_))
        await s.commit()


async def update_problem_statement(aid: str, text_: str, user_id: int | None = None) -> None:
    async with async_session() as s:
        q = update(Analysis).where(Analysis.id == aid)
        if user_id is not None:
            q = q.where(Analysis.user_id == user_id)
        await s.execute(q.values(problem_statement=text_))
        await s.commit()


async def update_importance(aid: str, importance: str, user_id: int | None = None) -> None:
    async with async_session() as s:
        q = update(Analysis).where(Analysis.id == aid)
        if user_id is not None:
            q = q.where(Analysis.user_id == user_id)
        await s.execute(q.values(importance=importance))
        await s.commit()


async def update_last_reviewed(aid: str) -> None:
    async with async_session() as s:
        await s.execute(
            update(Analysis).where(Analysis.id == aid).values(
                last_reviewed_at=datetime.now(timezone.utc).isoformat()
            )
        )
        await s.commit()


async def update_user_code(aid: str, code: str, user_id: int | None = None) -> None:
    async with async_session() as s:
        q = update(Analysis).where(Analysis.id == aid)
        if user_id is not None:
            q = q.where(Analysis.user_id == user_id)
        await s.execute(q.values(user_code=code))
        await s.commit()


async def update_lgm_info(
    aid: str, lgm_source: str, lgm_url: str, lgm_lang: str,
    lgm_sid: int, lgm_handle: str, user_id: int | None = None,
) -> None:
    async with async_session() as s:
        q = update(Analysis).where(Analysis.id == aid)
        if user_id is not None:
            q = q.where(Analysis.user_id == user_id)
        await s.execute(q.values(
            lgm_source=lgm_source, lgm_url=lgm_url, lgm_lang=lgm_lang,
            lgm_sid=lgm_sid, lgm_handle=lgm_handle,
        ))
        await s.commit()


async def list_analyses(user_id: int) -> list[dict]:
    async with async_session() as s:
        rows = (await s.execute(
            select(
                Analysis.id, Analysis.contest_id, Analysis.problem_index,
                Analysis.user_lang, Analysis.created_at, Analysis.analysis,
            )
            .where(Analysis.user_id == user_id)
            .order_by(Analysis.created_at.desc())
        )).all()
        return [r._asdict() for r in rows]


async def get_analysis(aid: str, user_id: int | None = None) -> dict | None:
    async with async_session() as s:
        q = select(Analysis).where(Analysis.id == aid)
        if user_id is not None:
            q = q.where(Analysis.user_id == user_id)
        row = (await s.execute(q)).scalar_one_or_none()
        if not row:
            return None
        return {c.name: getattr(row, c.name) for c in Analysis.__table__.columns}


async def get_analysis_by_problem(
    handle: str, contest_id: int, problem_index: str, user_id: int | None = None,
) -> dict | None:
    async with async_session() as s:
        q = select(Analysis).where(
            Analysis.handle == handle,
            Analysis.contest_id == contest_id,
            Analysis.problem_index == problem_index,
        )
        if user_id is not None:
            q = q.where(Analysis.user_id == user_id)
        row = (await s.execute(q)).scalar_one_or_none()
        if not row:
            return None
        return {c.name: getattr(row, c.name) for c in Analysis.__table__.columns}


async def get_or_create_analysis_stub(
    handle: str, contest_id: int, problem_index: str,
    user_submission_id: int | None, user_lang: str,
    created_at: str, analysis_id: str, user_id: int | None = None,
) -> dict:
    existing = await get_analysis_by_problem(handle, contest_id, problem_index, user_id=user_id)
    if existing:
        return existing

    record = {
        "id": analysis_id,
        "contest_id": contest_id,
        "problem_index": problem_index,
        "user_lang": user_lang,
        "user_code": "",
        "lgm_source": None,
        "lgm_url": None,
        "lgm_lang": None,
        "lgm_sid": None,
        "lgm_handle": None,
        "analysis": "",
        "notes": "",
        "created_at": created_at,
        "handle": handle,
        "user_submission_id": user_submission_id,
        "style_review": "",
        "problem_statement": "",
    }
    await insert_analysis(record, user_id=user_id)
    return await get_analysis_by_problem(handle, contest_id, problem_index, user_id=user_id) or record


async def delete_analysis(aid: str, user_id: int | None = None) -> None:
    async with async_session() as s:
        q = delete(Analysis).where(Analysis.id == aid)
        if user_id is not None:
            q = q.where(Analysis.user_id == user_id)
        await s.execute(q)
        await s.commit()


# ---------------------------------------------------------------------------
# Submissions CRUD
# ---------------------------------------------------------------------------

async def insert_submissions_batch(rows: list[dict], user_id: int | None = None) -> int:
    if not rows:
        return 0
    inserted = 0
    async with async_session() as s:
        for row in rows:
            existing = (await s.execute(
                select(Submission.id).where(Submission.id == row["id"])
            )).scalar_one_or_none()
            if existing is None:
                s.add(Submission(user_id=user_id, **row))
                inserted += 1
        await s.commit()
    return inserted


async def get_latest_submission_id(handle: str, user_id: int | None = None) -> int:
    async with async_session() as s:
        q = select(func.max(Submission.id)).where(Submission.handle == handle)
        if user_id is not None:
            q = q.where(Submission.user_id == user_id)
        result = (await s.execute(q)).scalar()
        return result or 0


async def get_submission(submission_id: int) -> dict | None:
    async with async_session() as s:
        row = (await s.execute(
            select(Submission).where(Submission.id == submission_id)
        )).scalar_one_or_none()
        if not row:
            return None
        return {c.name: getattr(row, c.name) for c in Submission.__table__.columns}


async def get_latest_ac_submission(
    handle: str, contest_id: int, problem_index: str, user_id: int | None = None,
) -> dict | None:
    async with async_session() as s:
        q = (
            select(Submission)
            .where(
                Submission.handle == handle,
                Submission.contest_id == contest_id,
                Submission.problem_index == problem_index,
                Submission.verdict == "OK",
            )
            .order_by(Submission.id.desc())
            .limit(1)
        )
        if user_id is not None:
            q = q.where(Submission.user_id == user_id)
        row = (await s.execute(q)).scalar_one_or_none()
        if not row:
            return None
        return {c.name: getattr(row, c.name) for c in Submission.__table__.columns}


async def get_latest_submission(
    handle: str, contest_id: int, problem_index: str, user_id: int | None = None,
) -> dict | None:
    async with async_session() as s:
        q = (
            select(Submission)
            .where(
                Submission.handle == handle,
                Submission.contest_id == contest_id,
                Submission.problem_index == problem_index,
            )
            .order_by(Submission.id.desc())
            .limit(1)
        )
        if user_id is not None:
            q = q.where(Submission.user_id == user_id)
        row = (await s.execute(q)).scalar_one_or_none()
        if not row:
            return None
        return {c.name: getattr(row, c.name) for c in Submission.__table__.columns}


async def get_pending_problems(handle: str, user_id: int | None = None) -> dict:
    async with async_session() as s:
        uid_filter = "AND s.user_id = :uid" if user_id else ""
        params: dict = {"handle": handle}
        if user_id is not None:
            params["uid"] = user_id

        ac_rows = (await s.execute(text(f"""
            SELECT
                s.contest_id, s.problem_index, s.problem_name, s.rating, s.tags,
                s.language, MAX(s.id) as latest_sid, MAX(s.submitted_at) as submitted_at,
                a.id as aid, COALESCE(a.style_review,'') as style_review
            FROM submissions s
            LEFT JOIN analyses a
                ON a.user_id = s.user_id
                AND a.contest_id = s.contest_id
                AND a.problem_index = s.problem_index
            WHERE s.handle = :handle {uid_filter} AND s.verdict = 'OK'
              AND (a.analysis IS NULL OR a.analysis = '')
            GROUP BY s.contest_id, s.problem_index, s.problem_name, s.rating,
                     s.tags, s.language, a.id, a.style_review
            ORDER BY submitted_at DESC
        """), params)).mappings().all()

        uid_sub_filter = "AND ac.user_id = :uid" if user_id else ""
        failed_rows = (await s.execute(text(f"""
            SELECT
                s.contest_id, s.problem_index, s.problem_name, s.rating, s.tags,
                s.language, MAX(s.id) as latest_sid, MAX(s.submitted_at) as submitted_at,
                COUNT(*) as attempt_count
            FROM submissions s
            WHERE s.handle = :handle {uid_filter}
              AND NOT EXISTS (
                  SELECT 1 FROM submissions ac
                  WHERE ac.handle = s.handle
                    AND ac.contest_id = s.contest_id
                    AND ac.problem_index = s.problem_index
                    AND ac.verdict = 'OK'
                    {uid_sub_filter}
              )
            GROUP BY s.contest_id, s.problem_index, s.problem_name, s.rating,
                     s.tags, s.language
            ORDER BY submitted_at DESC
        """), params)).mappings().all()

    return {
        "ac_unreviewed": [dict(r) for r in ac_rows],
        "failed": [dict(r) for r in failed_rows],
    }


async def get_reviewed_problems(
    handle: str, user_id: int | None = None, search: str = "",
) -> list[dict]:
    async with async_session() as s:
        like = f"%{search}%" if search else "%"
        uid_filter = "AND a.user_id = :uid" if user_id else ""
        params: dict = {"handle": handle, "like": like}
        if user_id is not None:
            params["uid"] = user_id

        rows = (await s.execute(text(f"""
            SELECT
                a.id as aid, a.contest_id, a.problem_index, a.analysis,
                a.style_review, a.notes, a.created_at, a.user_lang,
                a.importance, a.last_reviewed_at,
                COALESCE(s.problem_name, '') as problem_name,
                COALESCE(s.rating, 0) as rating,
                COALESCE(s.tags, '[]') as tags
            FROM analyses a
            LEFT JOIN submissions s
                ON s.handle = a.handle
                AND s.contest_id = a.contest_id
                AND s.problem_index = a.problem_index
                AND s.verdict = 'OK'
            WHERE a.handle = :handle {uid_filter} AND a.analysis != ''
              AND (
                  a.problem_index LIKE :like OR
                  CAST(a.contest_id AS TEXT) LIKE :like OR
                  COALESCE(s.problem_name, '') LIKE :like
              )
            GROUP BY a.id, a.contest_id, a.problem_index, a.analysis,
                     a.style_review, a.notes, a.created_at, a.user_lang,
                     a.importance, a.last_reviewed_at,
                     s.problem_name, s.rating, s.tags
            ORDER BY a.created_at DESC
        """), params)).mappings().all()

    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Sync state
# ---------------------------------------------------------------------------

async def get_sync_state(handle: str, user_id: int | None = None) -> dict:
    async with async_session() as s:
        q = select(SyncState).where(SyncState.handle == handle)
        if user_id is not None:
            q = q.where(SyncState.user_id == user_id)
        row = (await s.execute(q)).scalar_one_or_none()
        if row:
            return {c.name: getattr(row, c.name) for c in SyncState.__table__.columns}
        return {
            "handle": handle,
            "last_submission_id": 0,
            "last_sync_at": None,
            "cold_start_done": 0,
        }


async def update_sync_state(handle: str, user_id: int | None = None, **kwargs) -> None:
    async with async_session() as s:
        q = select(SyncState).where(SyncState.handle == handle)
        if user_id is not None:
            q = q.where(SyncState.user_id == user_id)
        row = (await s.execute(q)).scalar_one_or_none()

        if row:
            for k, v in kwargs.items():
                if hasattr(row, k):
                    setattr(row, k, v)
        else:
            data = {"handle": handle, "user_id": user_id}
            data.update(kwargs)
            s.add(SyncState(**data))
        await s.commit()


# ---------------------------------------------------------------------------
# Admin stats
# ---------------------------------------------------------------------------

async def count_users() -> int:
    async with async_session() as s:
        return (await s.execute(select(func.count(User.id)))).scalar() or 0


async def count_dau() -> int:
    """Users active in the last 24 hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    async with async_session() as s:
        return (await s.execute(
            select(func.count(User.id)).where(User.last_login_at >= cutoff)
        )).scalar() or 0


async def count_analyses() -> int:
    async with async_session() as s:
        return (await s.execute(
            select(func.count(Analysis.id)).where(Analysis.analysis != "")
        )).scalar() or 0


async def count_analyses_today() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with async_session() as s:
        return (await s.execute(
            select(func.count(Analysis.id)).where(
                Analysis.analysis != "",
                Analysis.created_at >= today,
            )
        )).scalar() or 0


async def get_recent_users(limit: int = 20) -> list[dict]:
    async with async_session() as s:
        rows = (await s.execute(
            select(
                User.id, User.github_login, User.github_avatar_url,
                User.created_at, User.last_login_at,
            )
            .order_by(User.last_login_at.desc())
            .limit(limit)
        )).all()
        return [r._asdict() for r in rows]
