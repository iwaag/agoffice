from typing import Protocol, Sequence

from agoffice_domain.schema import SessionInfo, SessionListInfo


class SessionModel(Protocol):
    id: str
    user_id: str
    project_id: str
    title: str
    task_started_at: object
    finished_at: object
    config: dict


def session_model_to_info(model: SessionModel) -> SessionInfo:
    return SessionInfo(id=model.id, title=model.title, task_started_at=model.task_started_at, finished_at=model.finished_at, config=model.config)


def session_models_to_list_info(session_squence: Sequence[SessionModel]) -> SessionListInfo:
    return SessionListInfo(sessions=[session_model_to_info(session) for session in session_squence])
