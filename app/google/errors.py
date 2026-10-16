"""What can go wrong reading Google, in two kinds the callers treat differently."""

from __future__ import annotations


class SignInNeeded(Exception):
    """The stored sign-in is missing, unreadable or no longer accepted by Google."""


class GoogleError(Exception):
    """Google could not be read just now (network, quota, a file no longer shared). `status` is
    the HTTP status when Google answered at all."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status

    @property
    def gone(self) -> bool:
        """The file itself is out of reach (unshared, deleted), not Google or the network."""
        return self.status in (403, 404)
