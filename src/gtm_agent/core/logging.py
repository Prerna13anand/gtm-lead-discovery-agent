"""Structured logging setup.

Every stage should log through `get_logger(__name__)` and bind stage-relevant
context (company_id, run_id, stage) via `.bind(...)` rather than interpolating
it into the message string — that's what keeps logs machine-parseable once a
real run ledger (spec §15) exists.
"""

import logging
import sys

import structlog

from gtm_agent.config import get_settings

_configured = False


def configure_logging() -> None:
    """Configure structlog + stdlib logging. Idempotent — safe to call multiple times."""
    global _configured
    if _configured:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: structlog.typing.Processor
    if settings.log_json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _configured = True


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    """Return a structlog logger, configuring logging on first use if needed."""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)
