from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import signal
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import click
from sqlalchemy import or_, select, update

from . import create_app
from .models import OutboxEvent, db, utcnow_naive

LOGGER = logging.getLogger("andeslog.outbox")
LEASE_SECONDS = 90
HTTP_TIMEOUT_SECONDS = 10
RETRYABLE_CLIENT_ERRORS = {401, 403, 404, 408, 425, 429}
FAILOVER_CLIENT_ERRORS = {408, 425, 429}
MAX_RECEIPT_BYTES = 4096
MAX_RETRY_DELAY_SECONDS = 60


def next_retry_at(attempt: int, now: datetime, headers=None) -> datetime:
    """Bound the normal delay and honor a longer valid Retry-After hint."""
    base = min(MAX_RETRY_DELAY_SECONDS, 5 * (2 ** min(max(attempt - 1, 0), 4)))
    delay = random.uniform(base * 0.8, base)
    target = now + timedelta(seconds=delay)
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return target
    value = value.strip()
    if value.isascii() and value.isdigit():
        try:
            advised = now + timedelta(seconds=int(value))
        except (ValueError, OverflowError):
            # An unrepresentably long delay must never be truncated to 60 s.
            advised = datetime.max
    else:
        try:
            advised = parsedate_to_datetime(value)
            if advised.tzinfo is None:
                return target
            advised = advised.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, TypeError, OverflowError):
            return target
    return max(target, advised)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_event_request(request, timeout):
    # Never send a signed event or its body to an unexpected redirect target.
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def serialize_event(row: OutboxEvent) -> bytes:
    event = {
        "id": row.id,
        "event_type": row.event_type,
        "occurred_at": row.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "source": "andeslog-erp",
        "aggregate_type": row.aggregate_type,
        "aggregate_id": row.aggregate_id,
        "payload": row.payload,
    }
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign_body(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def post_event(url: str, secret: str, body: bytes) -> None:
    event_id = json.loads(body)["id"]
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AndesLog-Outbox/1.0",
            "X-AndesLog-Signature": sign_body(body, secret),
        },
    )
    with open_event_request(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Azure returned HTTP {response.status}")
        raw = response.read(MAX_RECEIPT_BYTES + 1)
        if len(raw) > MAX_RECEIPT_BYTES:
            raise RuntimeError("Azure returned an oversized receipt")
        try:
            receipt = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("Azure returned an invalid receipt") from None
        if (not isinstance(receipt, dict) or receipt.get("id") != event_id
                or not isinstance(receipt.get("status"), str) or receipt["status"] not in {"stored", "duplicate"}):
            raise RuntimeError("Azure returned an unrecognized receipt")


def _finish_claim(event_id: str, attempt: int, values: dict) -> bool:
    """Only the current claim generation may finalize this event."""
    result = db.session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id, OutboxEvent.status == "processing", OutboxEvent.attempts == attempt)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return result.rowcount == 1


def _validate_ingest_url(url: str, *, allow_local_http: bool = False) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port  # Reject malformed ports before sending anything.
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path != "/api/erp/events"
                or (parsed.scheme != "https" and not (allow_local_http and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}))
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
    except ValueError:
        raise RuntimeError("Invalid regional ERP ingress URL") from None


def _endpoint_identity(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname.casefold(), parsed.port or (443 if parsed.scheme == "https" else 80)


def _claim_one(event_id: str | None = None) -> OutboxEvent | None:
    now = utcnow_naive()
    statement = select(OutboxEvent).where(
        or_(
            (OutboxEvent.status == "pending") & (OutboxEvent.available_at <= now),
            (OutboxEvent.status == "processing") & (OutboxEvent.locked_until <= now),
        )
    )
    if event_id:
        statement = statement.where(OutboxEvent.id == event_id)
    row = db.session.execute(
        statement
        .order_by(OutboxEvent.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if row is None:
        return None
    row.status = "processing"
    row.attempts += 1
    row.locked_until = now + timedelta(seconds=LEASE_SECONDS)
    db.session.commit()
    return row


def process_once(app, event_id: str | None = None) -> bool:
    """Claim and deliver one event, optionally targeting a specific event ID."""
    with app.app_context():
        url = app.config.get("ERP_AZURE_INGEST_URL", "").strip()
        fallback_url = app.config.get("ERP_AZURE_INGEST_FALLBACK_URL", "").strip()
        fallback_secret = app.config.get("ERP_AZURE_INGEST_FALLBACK_HMAC_SECRET", "")
        secret = app.config.get("ERP_AZURE_INGEST_HMAC_SECRET", "")
        if not url or not secret:
            raise RuntimeError("Azure ERP integration URL and HMAC secret must be configured")
        _validate_ingest_url(url, allow_local_http=app.testing)
        if fallback_url:
            if not fallback_secret:
                raise RuntimeError("Regional ERP ingress secret is required")
            _validate_ingest_url(fallback_url, allow_local_http=app.testing)
            if _endpoint_identity(fallback_url) == _endpoint_identity(url):
                raise RuntimeError("Regional ERP ingress URLs must differ")
        row = _claim_one(event_id)
        if row is None:
            db.session.remove()
            return False
        event_id = row.id
        attempt = row.attempts
        event_type = row.event_type
        body = serialize_event(row)

        permanent_error = False
        retry_headers = []
        failures = []
        destination = None
        for label, endpoint, endpoint_secret in [("primary", url, secret)] + ([("secondary", fallback_url, fallback_secret)] if fallback_url else []):
            try:
                post_event(endpoint, endpoint_secret, body)
            except HTTPError as exc:
                failures.append(f"{label}: HTTP {exc.code}")
                retry_headers.append(exc.headers)
                if label == "primary":
                    permanent_error = 400 <= exc.code < 500 and exc.code not in RETRYABLE_CLIENT_ERRORS
                    if exc.code not in FAILOVER_CLIENT_ERRORS and not 500 <= exc.code < 600:
                        break
            except (URLError, TimeoutError, OSError, RuntimeError) as exc:
                failures.append(f"{label}: {type(exc).__name__}")
            else:
                destination = label
                break

        if destination is not None:
            if _finish_claim(event_id, attempt, {
                "status": "delivered", "delivered_at": utcnow_naive(), "delivered_destination": destination,
                "locked_until": None, "last_error": None,
            }):
                LOGGER.info("Delivered ERP event %s (%s) to %s", event_id, event_type, destination)
            else:
                LOGGER.warning("Ignoring completion of stale outbox claim for %s", event_id)
            return True

        now = utcnow_naive()
        next_attempt = max([next_retry_at(attempt, now, headers) for headers in retry_headers] or [next_retry_at(attempt, now)])
        if _finish_claim(event_id, attempt, {
            "status": "dead" if permanent_error else "pending", "locked_until": None,
            "available_at": next_attempt, "last_error": "; ".join(failures)[:1000],
        }):
            LOGGER.warning("ERP event %s delivery deferred (attempt %s)", event_id, attempt)
        else:
            LOGGER.warning("Ignoring failure of stale outbox claim for %s", event_id)
        return True


def run_forever() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    app = create_app()
    stopping = False

    def stop(*_args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    poll_seconds = max(1, int(os.getenv("OUTBOX_POLL_SECONDS", "5")))
    while not stopping:
        try:
            did_work = process_once(app)
        except Exception:
            LOGGER.exception("Outbox worker iteration failed")
            time.sleep(poll_seconds)
            continue
        if not did_work:
            time.sleep(poll_seconds)


@click.command()
@click.option("--once", is_flag=True, help="Procesa como máximo un evento y sale.")
@click.option("--event-id", help="Procesa solo este ID de evento, para validación y reintento controlado.")
def main(once: bool, event_id: str | None) -> None:
    if once:
        app = create_app()
        process_once(app, event_id=event_id)
    else:
        if event_id:
            raise click.ClickException("--event-id solo puede usarse junto con --once")
        run_forever()


if __name__ == "__main__":
    main()
