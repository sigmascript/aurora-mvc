class HTTPException(Exception):
    """Raise an intentional HTTP error with a client-visible detail.

    Application converts this exception into a response using status_code
    and detail, for example HTTPException(404, "User not found").
    """

    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail

        super().__init__(detail)
