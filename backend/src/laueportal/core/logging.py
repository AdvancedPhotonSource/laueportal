"""Process-wide logging. Only entry points call ``configure_logging`` (the CLI, the app factory used by
``laueportal serve``, the worker); library modules just use ``logging.getLogger(__name__)``.
"""

from __future__ import annotations

import logging.config

from laueportal.core.settings import Settings

LOG_FORMAT = "%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s"


def configure_logging(settings: Settings) -> None:
    """Log to stderr (supervisor captures it) at ``settings.app.log_level``. Safe to call again."""
    level = settings.app.log_level.upper()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"plain": {"format": LOG_FORMAT}},
            "handlers": {
                "stderr": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr", "formatter": "plain"}
            },
            "root": {"level": level, "handlers": ["stderr"]},
            # uvicorn runs with log_config=None: its loggers propagate to the root handler.
            "loggers": {
                name: {"level": level, "handlers": [], "propagate": True}
                for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
            },
        }
    )
