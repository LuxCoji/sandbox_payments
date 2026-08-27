from prometheus_client import Counter, Gauge, Histogram, start_http_server

# -- Core Simulation Metrics --

EVENTS_PROCESSED = Counter(
    "finsim_events_processed_total",
    "Total number of domain events processed",
    ["event_type", "branch_id"]
)

TOOL_CALLS = Counter(
    "finsim_tool_calls_total",
    "Total number of tool gateway calls",
    ["tool_name", "actor_role"]
)

FORKS_CREATED = Counter(
    "finsim_forks_created_total",
    "Total number of branch forks created"
)

# -- Scheduler Performance --

SCHEDULER_QUEUE_SIZE = Gauge(
    "finsim_scheduler_queue_size",
    "Current number of events pending in the scheduler queue"
)

EVENT_LATENCY = Histogram(
    "finsim_event_processing_seconds",
    "Time spent processing a single domain event",
    ["event_type"]
)


def start_metrics_server(port: int = 8000) -> None:
    """Start the Prometheus metrics endpoint."""
    start_http_server(port)
