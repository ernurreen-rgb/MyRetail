from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from myretail_api.routers.auth import router as auth_router
from myretail_api.routers.health import router as health_router
from myretail_api.routers.products import router as products_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="MyRetail API",
        version="0.1.0",
        summary="Stable API gateway between MyRetail clients and ERPNext.",
    )
    app.add_exception_handler(StarletteHTTPException, product_http_exception_handler)
    app.add_exception_handler(RequestValidationError, product_validation_exception_handler)
    app.include_router(auth_router)
    app.include_router(health_router)
    app.include_router(products_router)
    return app


async def product_http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> Response:
    if not request.url.path.startswith("/products"):
        return await http_exception_handler(request, exc)

    detail = exc.detail
    if isinstance(detail, dict) and {"code", "message"}.issubset(detail):
        error = {
            "code": str(detail["code"]),
            "message": str(detail["message"]),
            "fields": detail.get("fields") if isinstance(detail.get("fields"), dict) else {},
        }
    else:
        error = _default_product_error(exc.status_code)

    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error},
        headers=getattr(exc, "headers", None),
    )


async def product_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> Response:
    if not request.url.path.startswith("/products"):
        return await request_validation_exception_handler(request, exc)

    fields: dict[str, str] = {}
    for error in exc.errors():
        location = [
            str(part)
            for part in error.get("loc", [])
            if part not in {"body", "query", "path"}
        ]
        if location:
            fields[".".join(location)] = "Некорректное значение"

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Проверьте поля товара",
                "fields": fields,
            }
        },
    )


def _default_product_error(status_code: int) -> dict[str, Any]:
    if status_code == status.HTTP_401_UNAUTHORIZED:
        return {
            "code": "AUTH_REQUIRED",
            "message": "Нужно войти в систему",
            "fields": {},
        }
    if status_code == status.HTTP_403_FORBIDDEN:
        return {
            "code": "FORBIDDEN",
            "message": "Недостаточно прав или неверный контекст тенанта",
            "fields": {},
        }
    return {
        "code": "REQUEST_ERROR",
        "message": "Запрос не может быть обработан",
        "fields": {},
    }


app = create_app()
