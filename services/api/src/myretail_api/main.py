from fastapi import FastAPI

from myretail_api.routers.health import router as health_router
from myretail_api.routers.products import router as products_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="MyRetail API",
        version="0.1.0",
        summary="Stable API gateway between MyRetail clients and ERPNext.",
    )
    app.include_router(health_router)
    app.include_router(products_router)
    return app


app = create_app()
