from datetime import datetime
from sqlalchemy import Engine
from sqlmodel import SQLModel, Session, create_engine, select

from agoffice_domain.schema import (
    NoobSessionCreateRequest,
    NoobSessionUpdate,
    NoobThreadCreateRequest,
    SessionConfig,
    SessionUpdate,
)
from agoffice_infra.config import get_database_settings
from agoffice_infra.db.models import (
    Agent,
    Instruction,
    NoobSession,
    NoobThread,
    Session as TaskSession,
    generate_session_id,
)

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(get_database_settings().url)
    return _engine


def init_database() -> None:
    SQLModel.metadata.create_all(get_engine())


def _allocate_session_index(session: Session, *, user_id: str, project_id: str) -> int:
    stmt = select(TaskSession.session_index).where(
        TaskSession.user_id == user_id,
        TaskSession.project_id == project_id,
    )
    used_indexes = set(session.exec(stmt).all())
    for session_index in range(101):
        if session_index not in used_indexes:
            return session_index
    raise ValueError(f"No available session index for user_id={user_id} project_id={project_id}")



def new_session(user_id: str, session_config: SessionConfig) -> TaskSession:
    with Session(get_engine()) as session:
        session_index = _allocate_session_index(session, user_id=user_id, project_id=session_config.project_id)
        new_session = TaskSession(
            id=generate_session_id(
                user_id=user_id,
                project_id=session_config.project_id,
                session_index=session_index,
            ),
            title=session_config.title,
            user_id=user_id,
            project_id=session_config.project_id,
            session_index=session_index,
            instruction=session_config.instruction,
            config=session_config.model_dump(),
            created_at=datetime.now(),
        )
        session.add(new_session)
        session.flush()
        session.commit()
        session.refresh(new_session)
        return new_session

def update_session(session_id: str, updates: SessionUpdate) -> TaskSession:
    with Session(get_engine()) as session:
        db_session = session.get(TaskSession, session_id)
        if not db_session:
            raise ValueError(f"Session {session_id} not found")
        update_data = updates.model_dump(exclude_unset=True)
        db_session.sqlmodel_update(update_data)
        db_session.updated_at = datetime.now()
        session.add(db_session)
        session.commit()
        session.refresh(db_session)
        return db_session

def get_session(session_id: str) -> TaskSession:
    with Session(get_engine()) as session:
        return session.get(TaskSession, session_id)


def get_noob_session(session_id: str) -> NoobSession | None:
    with Session(get_engine()) as session:
        return session.get(NoobSession, session_id)


def get_active_noob_session_for_user(user_id: str) -> NoobSession | None:
    with Session(get_engine()) as session:
        stmt = (
            select(NoobSession)
            .where(NoobSession.user_id == user_id, NoobSession.finished_at.is_(None))
            .order_by(NoobSession.created_at.desc())
        )
        return session.exec(stmt).first()


def new_noob_session(user_id: str, session_config: NoobSessionCreateRequest) -> NoobSession:
    new_session = NoobSession(
        title=session_config.title,
        user_id=user_id,
        project_id=session_config.project_id,
        initial_instruction=session_config.initial_instruction,
        config=session_config.model_dump(),
        created_at=datetime.now(),
    )
    with Session(get_engine()) as session:
        session.add(new_session)
        session.flush()
        session.commit()
        session.refresh(new_session)
        return new_session


def update_noob_session(session_id: str, updates: NoobSessionUpdate) -> NoobSession:
    with Session(get_engine()) as session:
        db_session = session.get(NoobSession, session_id)
        if not db_session:
            raise ValueError(f"NOOB session {session_id} not found")
        update_data = updates.model_dump(exclude_unset=True)
        db_session.sqlmodel_update(update_data)
        db_session.updated_at = datetime.now()
        session.add(db_session)
        session.commit()
        session.refresh(db_session)
        return db_session


def create_noob_thread(noob_session_id: str, thread: NoobThreadCreateRequest) -> NoobThread:
    new_thread = NoobThread(
        noob_session_id=noob_session_id,
        title=thread.title,
        keep_context=thread.keep_context,
        status="idle",
        created_at=datetime.now(),
    )
    with Session(get_engine()) as session:
        session.add(new_thread)
        session.flush()
        session.commit()
        session.refresh(new_thread)
        return new_thread


def list_noob_threads(noob_session_id: str) -> list[NoobThread]:
    with Session(get_engine()) as session:
        stmt = (
            select(NoobThread)
            .where(NoobThread.noob_session_id == noob_session_id)
            .order_by(NoobThread.created_at.asc())
        )
        return list(session.exec(stmt).all())


def get_noob_thread(thread_id: str) -> NoobThread | None:
    with Session(get_engine()) as session:
        return session.get(NoobThread, thread_id)


def get_active_noob_thread(noob_session_id: str) -> NoobThread | None:
    with Session(get_engine()) as session:
        stmt = (
            select(NoobThread)
            .where(NoobThread.noob_session_id == noob_session_id)
            .order_by(NoobThread.created_at.desc())
        )
        return session.exec(stmt).first()


def update_noob_thread_status(thread_id: str, status: str) -> NoobThread:
    with Session(get_engine()) as session:
        thread = session.get(NoobThread, thread_id)
        if not thread:
            raise ValueError(f"NOOB thread {thread_id} not found")
        thread.status = status
        thread.updated_at = datetime.now()
        session.add(thread)
        session.commit()
        session.refresh(thread)
        return thread

def list_sessions(user_id: str, project_id: str) -> list[TaskSession]:
    with Session(get_engine()) as session:
        stmt = select(TaskSession).where(TaskSession.user_id == user_id, TaskSession.project_id == project_id)
        return list(session.exec(stmt).all())
