from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SENSITIVE_API_PREFIXES = (
    "/auth",
    "/products",
    "/pos",
    "/stock",
    "/suppliers",
    "/purchases",
)
_SENSITIVE_CACHE_CONTROL = "private, no-store, max-age=0"
_SENSITIVE_CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'; sandbox"


class ApiSecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                apply_api_security_headers(headers, path=scope["path"])
            await send(message)

        await self._app(scope, receive, send_with_security_headers)


def _is_sensitive_api_path(path: str) -> bool:
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in _SENSITIVE_API_PREFIXES
    )


def apply_api_security_headers(
    headers: MutableHeaders,
    *,
    path: str,
) -> None:
    headers["X-Content-Type-Options"] = "nosniff"
    if _is_sensitive_api_path(path):
        headers["Cache-Control"] = _SENSITIVE_CACHE_CONTROL
        headers["Pragma"] = "no-cache"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Content-Security-Policy"] = _SENSITIVE_CONTENT_SECURITY_POLICY
