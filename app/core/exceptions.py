class AppException(Exception):
    def __init__(self, error_code: str, message: str, http_status: int = 500, module: str = "unknown"):
        self.error_code = error_code
        self.message = message
        self.http_status = http_status
        self.module = module
        super().__init__(message)