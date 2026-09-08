from uuid import UUID


class SessionNotFound(Exception):
    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id
