class TransferError(Exception):
    """A stable, safe-to-display failure (never include server responses/secrets)."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
