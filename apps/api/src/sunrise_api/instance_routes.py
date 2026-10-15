"""Публичные ассеты экземпляра: только известные роли и безопасные файлы."""

from fastapi import HTTPException, Response
from sunrise_infrastructure.instance_assets import read_asset


def register_instance_routes(app, settings):
    @app.get("/api/v1/instance/assets/{role}")
    def instance_asset(role: str):
        result = read_asset(settings.instance, role)
        if result is None:
            raise HTTPException(404)
        content, media_type = result
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )
