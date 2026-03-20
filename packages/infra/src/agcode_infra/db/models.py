from datetime import datetime
from typing import Any, Optional
import nanoid

from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.dialects.postgresql import JSONB
from uuid import UUID as PyUUID
from sqlmodel import Field, Column, SQLModel, String

def generate_nanoid() -> str:
    return nanoid.generate(size=12)

class Agent(SQLModel, table=True):
    id: str = Field(
        default_factory=generate_nanoid,
        sa_column=Column(String(12), primary_key=True, index=True, nullable=False),
    )
    name: str
    model: str

class Instruction(SQLModel, table=True):
    id: str = Field(
        default_factory=generate_nanoid,
        sa_column=Column(String(12), primary_key=True, index=True, nullable=False),
    )
    title: str
    content: str

class Session(SQLModel, table=True):
    id: str = Field(
        default_factory=generate_nanoid,
        sa_column=Column(String(12), primary_key=True, index=True, nullable=False),
    )
    title: str
    instruction: str
    created_at: datetime
    task_started_at: Optional[datetime]
    finished_at: Optional[datetime] = None
    updated_at: Optional[datetime]
    user_id: str = Field(index=True)
    project_id: str
    config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(MutableDict.as_mutable(JSONB), nullable=False),
    )


class NoobSession(SQLModel, table=True):
    id: str = Field(
        default_factory=generate_nanoid,
        sa_column=Column(String(12), primary_key=True, index=True, nullable=False),
    )
    title: str
    initial_instruction: str
    created_at: datetime
    finished_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    user_id: str = Field(index=True)
    project_id: str
    config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(MutableDict.as_mutable(JSONB), nullable=False),
    )


class NoobThread(SQLModel, table=True):
    id: str = Field(
        default_factory=generate_nanoid,
        sa_column=Column(String(12), primary_key=True, index=True, nullable=False),
    )
    noob_session_id: str = Field(index=True)
    title: Optional[str] = None
    keep_context: bool = True
    status: str = "idle"
    created_at: datetime
    updated_at: Optional[datetime] = None


class Mission(SQLModel, table=True):
    id: str = Field(
        default_factory=generate_nanoid,
        sa_column=Column(String(12), primary_key=True, index=True, nullable=False),
    )
    mission_name: str
    repo_url: str
    instruction: str
    session_id: Optional[str] = Field(default=None, index=True)
    user_id: str = Field(index=True)
    project_id: str = Field(index=True)
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
