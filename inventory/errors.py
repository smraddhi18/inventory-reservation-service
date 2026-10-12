class ApplicationError(Exception):
    status_code = 400
    code = "invalid_request"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code or self.code

    def payload(self):
        return {"error": {"code": self.code, "message": str(self)}}


class NotFoundError(ApplicationError):
    status_code = 404
    code = "not_found"


class ConflictError(ApplicationError):
    status_code = 409
    code = "conflict"


class UnauthorizedError(ApplicationError):
    status_code = 401
    code = "unauthorized"


class PayloadTooLargeError(ApplicationError):
    status_code = 413
    code = "body_too_large"


class UnexpectedError(ApplicationError):
    status_code = 500
    code = "internal_error"

    def __init__(self):
        super().__init__("An unexpected error occurred")
