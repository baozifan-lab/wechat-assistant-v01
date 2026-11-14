class TeamError(ValueError):
    def __init__(self, message, status=400, code='invalid_request'):
        super().__init__(message)
        self.status, self.code = status, code


def require(condition, message='没有权限执行此操作。', status=403, code='forbidden'):
    if not condition:
        raise TeamError(message, status, code)
