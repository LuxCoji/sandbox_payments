import structlog
from opentelemetry import trace


def get_logger(name: str) -> structlog.BoundLogger:
    """Return a structlog logger configured for JSON output with trace IDs."""
    
    def add_trace_id(logger, method_name, event_dict):
        span = trace.get_current_span()
        if span and span.is_recording():
            ctx = span.get_span_context()
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
        return event_dict

    if not structlog.is_configured():
        import logging
        import sys
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
        
        structlog.configure(
            processors=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                add_trace_id,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer()
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
    
    return structlog.get_logger(name)
