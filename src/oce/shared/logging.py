"""Logging configuration shared by the CLI and the ASGI application."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from oce.shared.config.settings import LogSettings

# Log context the CLI hands to the ASGI lifespan; not user configuration.
LOG_LEVEL_ENV = "OCE_LOG_LEVEL"
DATA_DIR_ENV = "OCE_DATA_DIR"


def configure_logging(
    log_settings: LogSettings,
    level: str | None = None,
    data_dir: Path | None = None,
) -> None:
    """Configure loguru; ``level`` overrides the configured level, ``data_dir`` locates the log file."""
    logger.remove()

    effective_level = level or log_settings.level

    logger.add(sys.stderr, level=effective_level)

    if log_settings.file_enabled:
        log_path = log_settings.file_path
        if log_path is None:
            # Personal mode logs under the data directory, service mode under
            # the working directory.
            if data_dir is not None:
                log_path = str(data_dir / "logs" / "oce.log")
            else:
                log_path = "logs/oce.log"

        Path(log_path).parent.mkdir(parents=True, exist_ok=True)

        if log_settings.format_json:
            logger.add(
                log_path,
                level=effective_level,
                rotation=log_settings.rotation,
                retention=log_settings.retention,
                serialize=True,
            )
        else:
            logger.add(
                log_path,
                level=effective_level,
                rotation=log_settings.rotation,
                retention=log_settings.retention,
                format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
            )
