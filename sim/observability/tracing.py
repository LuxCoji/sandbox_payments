import functools
from typing import Any, Callable

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_tracing(service_name: str = "finsim") -> None:
    """Initialize OpenTelemetry tracing with OTLP export."""
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    
    # Configure OTLP Exporter (defaults to localhost:4317 which matches Jaeger in docker-compose)
    exporter = OTLPSpanExporter()
    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)
    
    trace.set_tracer_provider(provider)


def traced(name: str | None = None) -> Callable:
    """Decorator to automatically trace a function or method.
    
    Usage:
        @traced("WorldEngine.execute_command")
        def execute_command(self, cmd):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = trace.get_tracer(__name__)
            span_name = name or func.__qualname__
            with tracer.start_as_current_span(span_name):
                return func(*args, **kwargs)
        return wrapper
    return decorator
