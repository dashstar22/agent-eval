"""不含底层异常及用户输入的安全业务错误。"""


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: list[dict] | None = None):
        self.status, self.code, self.message = status, code, message
        self.fields = fields or []
        super().__init__(code)
