from typing import Protocol, Sequence

from agcode_domain.schema import SessionInfo, SessionListInfo


class SessionProjection(Protocol):
    id: str
    title: str
    task_started_at: object
    finished_at: object
    config: dict


def session_model_to_scheme(model: SessionProjection) -> SessionInfo:
    return SessionInfo(id=model.id, title=model.title, task_started_at=model.task_started_at, finished_at=model.finished_at, config=model.config)


def session_model_sequence_to_sceme(session_squence: Sequence[SessionProjection]) -> SessionListInfo:
    return SessionListInfo(sessions=[session_model_to_scheme(session) for session in session_squence])
