from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from myretail_api.config import Settings, get_settings, validate_production_state_storage
from myretail_api.http_security import (
    ApiSecurityHeadersMiddleware,
    apply_api_security_headers,
)
from myretail_api.routers.auth import router as auth_router
from myretail_api.routers.health import router as health_router
from myretail_api.routers.pos import router as pos_router
from myretail_api.routers.products import router as products_router
from myretail_api.routers.purchases import purchases_router, suppliers_router
from myretail_api.routers.stock import router as stock_router


def create_app(settings: Settings | None = None) -> FastAPI:
    validate_production_state_storage(settings or get_settings())
    app = FastAPI(
        title="MyRetail API",
        version="0.1.0",
        summary="Stable API gateway between MyRetail clients and ERPNext.",
    )
    app.add_middleware(ApiSecurityHeadersMiddleware)
    app.add_exception_handler(Exception, internal_server_error_handler)
    app.add_exception_handler(StarletteHTTPException, product_http_exception_handler)
    app.add_exception_handler(RequestValidationError, product_validation_exception_handler)
    app.include_router(auth_router)
    app.include_router(health_router)
    app.include_router(products_router)
    app.include_router(pos_router)
    app.include_router(stock_router)
    app.include_router(suppliers_router)
    app.include_router(purchases_router)
    return app


async def internal_server_error_handler(request: Request, exc: Exception) -> Response:
    del exc
    response = PlainTextResponse(
        "Internal Server Error",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
    apply_api_security_headers(response.headers, path=request.url.path)
    return response


async def product_http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> Response:
    if not _uses_api_error_contract(request.url.path):
        return await http_exception_handler(request, exc)

    detail = exc.detail
    if isinstance(detail, dict) and {"code", "message"}.issubset(detail):
        error = {
            "code": str(detail["code"]),
            "message": str(detail["message"]),
            "fields": detail.get("fields") if isinstance(detail.get("fields"), dict) else {},
        }
    elif request.url.path.startswith("/stock"):
        error = _default_stock_error(exc.status_code)
    elif _uses_purchase_error_contract(request.url.path):
        error = _default_purchase_error(exc.status_code)
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
    if not _uses_api_error_contract(request.url.path):
        return await request_validation_exception_handler(request, exc)

    if _is_missing_pos_return_idempotency_key(request, exc):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Заголовок Idempotency-Key обязателен",
                    "fields": {"Idempotency-Key": "Обязателен"},
                }
            },
        )

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
                "message": _validation_message(request.url.path),
                "fields": fields,
            }
        },
    )


def _uses_api_error_contract(path: str) -> bool:
    return (
        path.startswith("/products")
        or path.startswith("/pos")
        or path.startswith("/stock")
        or _uses_purchase_error_contract(path)
    )


def _validation_message(path: str) -> str:
    if _uses_purchase_error_contract(path):
        return "Проверьте поля закупки"
    if path.startswith("/pos"):
        return "Проверьте поля кассового запроса"
    if path.startswith("/stock"):
        return "Проверьте поля складской операции"
    return "Проверьте поля товара"


def _is_missing_pos_return_idempotency_key(
    request: Request, exc: RequestValidationError
) -> bool:
    path = request.url.path
    if request.method != "POST" or not (
        path == "/pos/returns" or (path.startswith("/pos/returns/") and path.endswith("/cancel"))
    ):
        return False
    return any(
        error.get("type") == "missing"
        and tuple(error.get("loc", ())) == ("header", "Idempotency-Key")
        for error in exc.errors()
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


def _default_stock_error(status_code: int) -> dict[str, Any]:
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
        "code": "INVALID_REQUEST",
        "message": "Запрос не может быть обработан",
        "fields": {},
    }


def _uses_purchase_error_contract(path: str) -> bool:
    return path.startswith("/suppliers") or path.startswith("/purchases")


def _default_purchase_error(status_code: int) -> dict[str, Any]:
    if status_code == status.HTTP_401_UNAUTHORIZED:
        return {
            "code": "UNAUTHORIZED",
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
        "code": "INVALID_REQUEST",
        "message": "Запрос не может быть обработан",
        "fields": {},
    }


app = create_app()
