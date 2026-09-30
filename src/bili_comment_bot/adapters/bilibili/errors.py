"""Sanitized platform failures: never retain response text, request URLs or cookies."""

import asyncio


class PlatformError(Exception):
    def __init__(self, code: int | None = None):
        self.code = code
        super().__init__(f"{type(self).__name__} (code={code})")


class LoginExpired(PlatformError):
    pass


class CaptchaRequired(PlatformError):
    pass


class RateLimited(PlatformError):
    pass


class HTTPFault(PlatformError):
    pass


class ProtocolFault(PlatformError):
    pass


class QRLoginProtocolFault(ProtocolFault):
    """Malformed QR fields or an untrusted display URL; never retains the payload."""


class NetworkFault(PlatformError):
    pass


class ReauthenticationRequired(PlatformError):
    pass


class IdentityMismatch(PlatformError):
    pass


AUTH_FAILURES = (LoginExpired, CaptchaRequired, ReauthenticationRequired, IdentityMismatch)


class AuthFault:
    """Sticky, process-local stop signal. Never carries requests or credentials."""

    def __init__(self):
        self.event = asyncio.Event()
        self.kind: type[PlatformError] | None = None

    def notify(self, error: BaseException):
        if isinstance(error, AUTH_FAILURES) and self.kind is None:
            self.kind = type(error)
            self.event.set()

    def check(self):
        if self.kind is not None:
            raise self.kind()


def business_error(code: int) -> PlatformError:
    if code in {-101, -102}:
        return LoginExpired(code)
    if code in {-105, -352, 12015}:
        return CaptchaRequired(code)
    if code == -509:
        return RateLimited(code)
    return PlatformError(code)
