from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

class AgentDeployment(BaseModel):
    agent_id: str
    instruction: str

class SessionConfig(BaseModel):
    agent_deployments: List[AgentDeployment]
    title: str
    project_id: str
    instruction: str

class AgentConfig(BaseModel):
    name: str
    model: str

class SessionUpdate(BaseModel):
    title: Optional[str] = None
    task_started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    config: Optional[Dict[str, Any]] = None

class SessionInfo(SessionUpdate):
    id: str

class SessionListInfo(BaseModel):
    sessions: List[SessionInfo]


class TunnelInfo(BaseModel):
    tunnel_name: str


class MissionCreateRequest(BaseModel):
    title: str = Field(min_length=1)
    repo_url: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    project_id: str = Field(min_length=1)


class MissionStartRequest(BaseModel):
    session_id: Optional[str] = None
    mission_id: str = Field(min_length=1)


class MissionInfo(BaseModel):
    id: str
    title: str
    repo_url: str
    instruction: str
    session_id: Optional[str] = None
    user_id: str
    project_id: str
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

class MissionUpdate(BaseModel):
    title: Optional[str] = None
    repo_url: Optional[str] = None
    instruction: Optional[str] = None

class MissionListInfo(BaseModel):
    missions: Sequence[MissionInfo]


class NoobWorkspacePrepSpec(BaseModel):
    repo_url: str = Field(min_length=1)
    ref: Optional[str] = None
    depth: Optional[int] = Field(default=None, ge=1)
    sub_path: Optional[str] = None
    context_files: List[str] = Field(default_factory=list)
    mode: str = "clone"


class NoobSessionCreateRequest(BaseModel):
    title: str
    project_id: str
    initial_instruction: str = Field(min_length=1)
    prep: Optional[NoobWorkspacePrepSpec] = None
    keep_context_default: bool = True


class NoobSessionUpdate(BaseModel):
    title: Optional[str] = None
    finished_at: Optional[datetime] = None
    config: Optional[Dict[str, Any]] = None


class NoobSessionInfo(NoobSessionUpdate):
    id: str
    user_id: str
    project_id: str
    initial_instruction: str
    created_at: datetime
    updated_at: Optional[datetime] = None


class NoobThreadCreateRequest(BaseModel):
    title: Optional[str] = None
    keep_context: bool = True


class NoobThreadInfo(BaseModel):
    id: str
    noob_session_id: str
    title: Optional[str] = None
    keep_context: bool = True
    status: str = "idle"
    created_at: datetime
    updated_at: Optional[datetime] = None


class NoobWorkspacePrepRequest(BaseModel):
    spec: NoobWorkspacePrepSpec


class NoobWorkspacePrepStatus(BaseModel):
    status: str
    updated_at: Optional[datetime] = None
    error: Optional[str] = None
    workspace_path: Optional[str] = None
    repo_url: Optional[str] = None
    ref: Optional[str] = None


class NoobWorkspacePrepAcceptedResponse(BaseModel):
    status: str
    job_name: Optional[str] = None


class NoobTaskRequest(BaseModel):
    instruction: str = Field(min_length=1)
    context_file_paths: List[str] = Field(default_factory=list)
    workspace_path: Optional[str] = None
    output_file_path: str = "artifacts/response.md"
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    thread_id: Optional[str] = None


class NoobThreadRequest(BaseModel):
    instruction: str = Field(min_length=1)
    context_file_paths: List[str] = Field(default_factory=list)
    workspace_path: Optional[str] = None
    output_file_path: str = "artifacts/response.md"
    system_prompt: Optional[str] = None
    model: Optional[str] = None


class NoobTaskAcceptedResponse(BaseModel):
    status: str


class NoobTaskStatus(BaseModel):
    status: str
    updated_at: Optional[datetime] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None


class NoobTaskResult(BaseModel):
    exit_code: Optional[int] = None
    completed_at: Optional[datetime] = None
    output_path: Optional[str] = None
    content: Optional[str] = None
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    error: Optional[str] = None
    thread_id: Optional[str] = None


class NoobTaskEvent(BaseModel):
    timestamp: datetime
    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class NoobTaskEvents(BaseModel):
    events: List[NoobTaskEvent]
