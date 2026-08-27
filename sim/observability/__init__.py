"""Observability subsystem for tracing, metrics, and structured logging."""

from .logging import get_logger
from .metrics import (
    EVENT_LATENCY,
    EVENTS_PROCESSED,
    FORKS_CREATED,
    SCHEDULER_QUEUE_SIZE,
    TOOL_CALLS,
    start_metrics_server,
)
from .tracing import setup_tracing, traced

__all__ = [
    "get_logger",
    "setup_tracing",
    "traced",
    "EVENTS_PROCESSED",
    "TOOL_CALLS",
    "FORKS_CREATED",
    "SCHEDULER_QUEUE_SIZE",
    "EVENT_LATENCY",
    "start_metrics_server",
]
