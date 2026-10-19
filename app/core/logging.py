import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

# Set per request by the middleware so every log line can carry the correlation ID.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

# Extra fields a log call may attach via `extra={...}`.
_EXTRA_FIELDS = ("method", "path", "status_code", "latency_ms")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }
        for field in _EXTRA_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Uvicorn's own access log would duplicate our request log line.
    logging.getLogger("uvicorn.access").disabled = True
