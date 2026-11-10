from __future__ import annotations

from typing import Any

from .models import OutboxEvent, db


def enqueue_event(
    event_type: str,
    aggregate_type: str,
    aggregate_id: str | int,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
) -> OutboxEvent:
    """Add an integration event to the same transaction as the ERP change."""
    event = OutboxEvent(
        **({"id": event_id} if event_id is not None else {}),
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        payload=payload,
    )
    db.session.add(event)
    return event
