class DomainError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 409,
        retryable: bool = False,
        next_action: str = "refresh",
    ):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.retryable, self.next_action = retryable, next_action


def missing():
    return DomainError("not_found", "Страница не найдена.", 404, next_action="back")
