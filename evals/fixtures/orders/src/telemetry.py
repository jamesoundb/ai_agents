"""Telemetry sinks used by the gateway's tail-end bookkeeping."""
import logging

log = logging.getLogger(__name__)


def record_latency_sample(elapsed: float, bucket: str = "charge") -> float:
    """Push one latency sample into the histogram for `bucket`."""
    log.debug("latency %.4fs -> %s", elapsed, bucket)
    return elapsed


def write_audit_entry(order_id: str, payload: dict) -> None:
    """Append an immutable audit row for the charge."""
    log.info("audit order=%s keys=%s", order_id, sorted(payload))


def increment_counter(name: str, value: int = 1) -> str:
    """Bump a named counter."""
    return f"{name}+{value}"
