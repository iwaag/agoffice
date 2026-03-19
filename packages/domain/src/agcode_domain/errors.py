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
