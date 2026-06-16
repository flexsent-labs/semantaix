from fastapi import FastAPI

from platform_common.logging_config import configure_logging
from platform_common.settings import get_settings


def create_service_app(service_name: str, *, lifespan=None) -> FastAPI:
    configure_logging(service_name)
    settings = get_settings()
    app = FastAPI(title=f"semantaix-{service_name}", lifespan=lifespan)

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok", "service": service_name}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {
            "status": "ok",
            "service": service_name,
            "qdrant_url": settings.qdrant_url,
            "app_env": settings.app_env,
        }

    @app.get("/health/startup")
    def startup() -> dict[str, str]:
        return {"status": "ok", "service": service_name, "log_level": settings.log_level}

    return app
