"""Sanitized platform failures: never retain response text, request URLs or cookies."""


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


class NetworkFault(PlatformError):
    pass


class ReauthenticationRequired(PlatformError):
    pass


class IdentityMismatch(PlatformError):
    pass


def business_error(code: int) -> PlatformError:
    if code in {-101, -102}:
        return LoginExpired(code)
    if code in {-105, -352, 12015}:
        return CaptchaRequired(code)
    if code == -509:
        return RateLimited(code)
    return PlatformError(code)
