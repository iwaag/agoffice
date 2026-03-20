class SessionError(Exception):
    pass


class SessionNotFoundError(SessionError):
    pass


class SessionAccessDeniedError(SessionError):
    pass


class NoobSessionConflictError(SessionError):
    pass


class NoobThreadNotFoundError(SessionError):
    pass


class MissionNotFoundError(SessionError):
    pass


class MissionAccessDeniedError(SessionError):
    pass


class MissionConflictError(SessionError):
    pass
