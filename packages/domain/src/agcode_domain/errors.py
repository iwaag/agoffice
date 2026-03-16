class SessionError(Exception):
    pass


class SessionNotFoundError(SessionError):
    pass


class SessionAccessDeniedError(SessionError):
    pass
