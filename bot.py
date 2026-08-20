# Compatibility entrypoint for Render.
# This allows the Start Command `gunicorn bot:app` to work.
from bot_render import app

__all__ = ["app"]
