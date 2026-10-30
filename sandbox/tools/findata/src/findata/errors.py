class DataError(Exception):
    """Public source failure or invalid market-data request."""

    def __init__(self, message, *, attempts=None):
        super().__init__(message)
        self.attempts = attempts or []

    def as_dict(self):
        result = {"error": {"type": type(self).__name__, "message": str(self)}}
        if self.attempts:
            result["error"]["attempts"] = self.attempts
        return result
