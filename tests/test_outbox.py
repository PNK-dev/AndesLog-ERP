from datetime import datetime, timedelta
import json
from urllib.error import HTTPError, URLError

import pytest
from app import create_app
from app.models import OutboxEvent, User, db, utcnow_naive
from app.outbox import enqueue_event
from app.outbox_worker import process_once, sign_body


@pytest.fixture()
def app(tmp_path):
    instance = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret-key",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'outbox-test.db'}",
        "WTF_CSRF_ENABLED": False,
        "SESSION_COOKIE_SECURE": False,
    })
    with instance.app_context():
        db.create_all()
        admin = User(username="admin", role="admin")
        admin.set_password("Test-password-2026!")
        db.session.add(admin)
        db.session.commit()
        yield instance
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


class FakeResponse:
    status = 202

    def __init__(self, request, receipt=None):
        self.receipt = receipt if receipt is not None else json.dumps({"status": "stored", "id": json.loads(request.data)["id"]}).encode()

    def read(self, size):
        return self.receipt[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _login(client):
    return client.post("/login", data={"username": "admin", "password": "Test-password-2026!"})


def test_event_is_committed_with_the_order(app, client):
    from app.models import Customer, Product
    _login(client)
    with app.app_context():
        customer = Customer(tax_id="20100000001", business_name="Cliente de prueba")
        product = Product(sku="OUTBOX-01", name="Carga de prueba")
        db.session.add_all([customer, product])
        db.session.commit()
        customer_id, product_id = customer.id, product.id

    response = client.post("/orders", data={
        "number": "OUTBOX-ORDER-01",
        "customer_id": customer_id,
        "product_id": product_id,
        "quantity": "2",
    })

    assert response.status_code == 302
    with app.app_context():
        event = OutboxEvent.query.filter_by(event_type="erp.order.created").one()
        assert event.status == "pending"
        assert event.payload["number"] == "OUTBOX-ORDER-01"
        assert event.payload["lines"] == [{"sku": "OUTBOX-01", "quantity": "2.00"}]


def test_worker_signs_and_marks_successful_delivery(app, client, monkeypatch):
    from app.models import Customer, Product
    import app.outbox_worker as worker

    _login(client)
    with app.app_context():
        customer = Customer(tax_id="20100000002", business_name="Cliente worker")
        product = Product(sku="OUTBOX-02", name="Worker")
        db.session.add_all([customer, product])
        db.session.commit()
        customer_id, product_id = customer.id, product.id
    client.post("/orders", data={
        "number": "OUTBOX-ORDER-02",
        "customer_id": customer_id,
        "product_id": product_id,
        "quantity": "1",
    })

    app.config.update(
        ERP_AZURE_INGEST_URL="https://example.azurewebsites.net/api/erp/events",
        ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret",
    )
    captured = {}

    def fake_urlopen(request, timeout):
        captured["signature"] = request.get_header("X-andeslog-signature")
        captured["body"] = request.data
        captured["timeout"] = timeout
        return FakeResponse(request)

    monkeypatch.setattr(worker, "open_event_request", fake_urlopen)
    assert process_once(app) is True
    assert captured["signature"] == sign_body(captured["body"], "test-integration-secret")
    assert captured["timeout"] == worker.HTTP_TIMEOUT_SECONDS
    with app.app_context():
        event = OutboxEvent.query.filter_by(event_type="erp.order.created").one()
        assert event.status == "delivered"
        assert event.delivered_at is not None


def test_worker_requeues_a_transient_delivery_failure(app, monkeypatch):
    import app.outbox_worker as worker

    app.config.update(
        ERP_AZURE_INGEST_URL="https://example.azurewebsites.net/api/erp/events",
        ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret",
    )
    with app.app_context():
        enqueue_event("erp.test", "test", "1", {"value": "safe"})
        db.session.commit()

    def fail_urlopen(*_args, **_kwargs):
        raise URLError("temporary network failure")

    monkeypatch.setattr(worker, "open_event_request", fail_urlopen)
    assert process_once(app) is True
    with app.app_context():
        event = OutboxEvent.query.one()
        assert event.status == "pending"
        assert event.attempts == 1
        assert event.available_at > utcnow_naive() + timedelta(seconds=3)
        assert "URLError" in event.last_error
        assert "test-integration-secret" not in event.last_error


def test_worker_keeps_retrying_transient_failures_after_many_attempts(app, monkeypatch):
    import app.outbox_worker as worker

    app.config.update(
        ERP_AZURE_INGEST_URL="https://example.azurewebsites.net/api/erp/events",
        ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret",
    )
    with app.app_context():
        event = enqueue_event("erp.test", "test", "long-outage", {"value": "durable"})
        event.attempts = 12
        event.available_at = utcnow_naive() - timedelta(seconds=1)
        db.session.commit()
        event_id = event.id

    def fail_with_service_unavailable(*_args, **_kwargs):
        raise HTTPError("https://example.azurewebsites.net/api/erp/events", 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(worker, "open_event_request", fail_with_service_unavailable)
    assert process_once(app) is True
    with app.app_context():
        event = db.session.get(OutboxEvent, event_id)
        assert event.status == "pending"
        assert event.attempts == 13
        assert utcnow_naive() + timedelta(seconds=47) < event.available_at <= utcnow_naive() + timedelta(seconds=60)


def test_retry_after_header_overrides_normal_limit(app, monkeypatch):
    import app.outbox_worker as worker

    app.config.update(ERP_AZURE_INGEST_URL="https://example.azurewebsites.net/api/erp/events",
                      ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret")
    with app.app_context():
        event = enqueue_event("erp.test", "test", "throttle", {})
        db.session.commit()
        event_id = event.id

    def throttle(*_args, **_kwargs):
        raise HTTPError("https://example.azurewebsites.net", 429, "Throttled", {"Retry-After": "300"}, None)

    monkeypatch.setattr(worker, "open_event_request", throttle)
    assert process_once(app) is True
    with app.app_context():
        event = db.session.get(OutboxEvent, event_id)
        assert event.status == "pending"
        assert event.available_at >= utcnow_naive() + timedelta(seconds=299)


def test_retry_after_date_invalid_and_very_large_values(monkeypatch):
    import app.outbox_worker as worker

    monkeypatch.setattr(worker.random, "uniform", lambda _lower, upper: upper)
    now = datetime(2026, 11, 10, 12, 0, 0)
    assert worker.next_retry_at(20, now) == now + timedelta(seconds=60)
    assert worker.next_retry_at(20, now, {"Retry-After": "Tue, 10 Nov 2026 12:05:00 GMT"}) == now + timedelta(minutes=5)
    assert worker.next_retry_at(20, now, {"Retry-After": "invalid"}) == now + timedelta(seconds=60)
    assert worker.next_retry_at(20, now, {"Retry-After": "999999999999999999999"}) == datetime.max


def test_long_outage_retries_then_delivers_after_service_recovers(app, monkeypatch):
    import app.outbox_worker as worker

    app.config.update(ERP_AZURE_INGEST_URL="https://example.azurewebsites.net/api/erp/events",
                      ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret")
    clock = [datetime(2026, 11, 10, 12, 0, 0)]
    monkeypatch.setattr(worker, "utcnow_naive", lambda: clock[0])
    monkeypatch.setattr(worker.random, "uniform", lambda _lower, upper: upper)
    with app.app_context():
        event = enqueue_event("erp.test", "test", "long-outage", {})
        event.attempts = 20
        event.available_at = clock[0] - timedelta(seconds=1)
        db.session.commit()
        event_id = event.id
    calls = [0]

    def recover(request, **_):
        calls[0] += 1
        if calls[0] == 1:
            raise HTTPError("https://example.azurewebsites.net", 503, "Unavailable", {}, None)
        return FakeResponse(request)

    monkeypatch.setattr(worker, "open_event_request", recover)
    assert process_once(app, event_id)
    with app.app_context():
        event = db.session.get(OutboxEvent, event_id)
        assert event.status == "pending"
        assert event.available_at == clock[0] + timedelta(seconds=60)
    clock[0] += timedelta(seconds=61)
    assert process_once(app, event_id)
    with app.app_context():
        assert db.session.get(OutboxEvent, event_id).status == "delivered"


def test_permanent_event_rejection_can_be_manually_requeued(app, monkeypatch):
    import app.outbox_worker as worker

    app.config.update(
        ERP_AZURE_INGEST_URL="https://example.azurewebsites.net/api/erp/events",
        ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret",
    )
    with app.app_context():
        event = enqueue_event("erp.test", "test", "bad-payload", {"value": "review"})
        db.session.commit()
        event_id = event.id

    def fail_with_bad_request(*_args, **_kwargs):
        raise HTTPError("https://example.azurewebsites.net/api/erp/events", 400, "Bad Request", {}, None)

    monkeypatch.setattr(worker, "open_event_request", fail_with_bad_request)
    assert process_once(app) is True
    with app.app_context():
        assert db.session.get(OutboxEvent, event_id).status == "dead"

    result = app.test_cli_runner().invoke(args=["outbox-retry-dead", "--event-id", event_id])
    assert result.exit_code == 0
    assert result.output.strip() == "pending"
    with app.app_context():
        event = db.session.get(OutboxEvent, event_id)
        assert event.status == "pending"
        assert event.available_at <= utcnow_naive() + timedelta(seconds=1)


def test_outbox_smoke_cli_creates_and_reports_a_pending_event(app):
    runner = app.test_cli_runner()
    created = runner.invoke(args=["outbox-smoke"])
    assert created.exit_code == 0
    event_id = created.output.strip()
    assert len(event_id) == 36
    status = runner.invoke(args=["outbox-status", "--event-id", event_id])
    assert status.exit_code == 0
    assert status.output.strip() == "pending"


def test_worker_can_process_only_the_requested_event(app, monkeypatch):
    import app.outbox_worker as worker

    app.config.update(
        ERP_AZURE_INGEST_URL="https://example.azurewebsites.net/api/erp/events",
        ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret",
    )
    with app.app_context():
        older = enqueue_event("erp.test", "test", "older", {"value": "leave-pending"})
        target = enqueue_event("erp.test", "test", "target", {"value": "deliver"})
        db.session.commit()
        older_id = older.id
        target_id = target.id

    monkeypatch.setattr(worker, "open_event_request", lambda request, **_kwargs: FakeResponse(request))
    assert process_once(app, event_id=target_id) is True
    with app.app_context():
        assert db.session.get(OutboxEvent, target_id).status == "delivered"
        assert db.session.get(OutboxEvent, older_id).status == "pending"


@pytest.mark.parametrize("receipt", [b"", b"not-json", b'{"status":"stored","id":"wrong"}',
                                     b'{"status":"ok"}', b'{"status":[],"id":"wrong"}', b"x" * 4097])
def test_worker_retries_untrusted_success_receipts(app, monkeypatch, receipt):
    import app.outbox_worker as worker

    app.config.update(ERP_AZURE_INGEST_URL="https://example.azurewebsites.net/api/erp/events",
                      ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret")
    with app.app_context():
        event = enqueue_event("erp.test", "test", "receipt", {})
        db.session.commit()
        event_id = event.id
    monkeypatch.setattr(worker, "open_event_request", lambda request, **_: FakeResponse(request, receipt))
    assert process_once(app) is True
    with app.app_context():
        assert db.session.get(OutboxEvent, event_id).status == "pending"


def test_worker_accepts_matching_duplicate_receipt(app, monkeypatch):
    import app.outbox_worker as worker

    app.config.update(ERP_AZURE_INGEST_URL="https://example.azurewebsites.net/api/erp/events",
                      ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret")
    with app.app_context():
        event = enqueue_event("erp.test", "test", "duplicate", {})
        db.session.commit()
        event_id = event.id

    def duplicate(request, **_):
        return FakeResponse(request, json.dumps({"status": "duplicate", "id": event_id}).encode())

    monkeypatch.setattr(worker, "open_event_request", duplicate)
    assert process_once(app) is True
    with app.app_context():
        assert db.session.get(OutboxEvent, event_id).status == "delivered"


def test_worker_retries_receipt_with_non_string_status(app, monkeypatch):
    import app.outbox_worker as worker

    app.config.update(ERP_AZURE_INGEST_URL="https://example.azurewebsites.net/api/erp/events",
                      ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret")
    with app.app_context():
        event = enqueue_event("erp.test", "test", "malformed-receipt", {})
        db.session.commit()
        event_id = event.id
    monkeypatch.setattr(worker, "open_event_request", lambda request, **_: FakeResponse(
        request, json.dumps({"status": [], "id": event_id}).encode()))
    assert process_once(app) is True
    with app.app_context():
        assert db.session.get(OutboxEvent, event_id).status == "pending"


@pytest.mark.parametrize("older_result,newer_status", [("success", "processing"), ("permanent", "delivered"), ("temporary", "delivered")])
def test_stale_worker_cannot_overwrite_newer_claim(app, monkeypatch, older_result, newer_status):
    import app.outbox_worker as worker

    app.config.update(ERP_AZURE_INGEST_URL="https://example.azurewebsites.net/api/erp/events",
                      ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret")
    with app.app_context():
        event = enqueue_event("erp.test", "test", "lease", {})
        db.session.commit()
        event_id = event.id

    def concurrent_delivery(*_):
        # A second claim takes over while the first worker is still waiting.
        with app.app_context():
            current = db.session.get(OutboxEvent, event_id)
            current.attempts = 2
            current.status = newer_status
            db.session.commit()
        if older_result == "permanent":
            raise HTTPError("https://example.azurewebsites.net", 400, "Bad Request", {}, None)
        if older_result == "temporary":
            raise URLError("temporary error")

    monkeypatch.setattr(worker, "post_event", concurrent_delivery)
    assert process_once(app) is True
    with app.app_context():
        current = db.session.get(OutboxEvent, event_id)
        assert current.status == newer_status
        assert current.attempts == 2


def test_http_redirect_does_not_forward_signed_event():
    import http.server
    import threading
    from app.outbox_worker import post_event

    class Sink(http.server.BaseHTTPRequestHandler):
        calls = 0

        def do_POST(self):
            type(self).calls += 1
            self.send_response(200)
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *_):
            pass

    sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/elsewhere")
            self.end_headers()

        def log_message(self, *_):
            pass

    redirect = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    servers = (sink, redirect)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        with pytest.raises(HTTPError) as error:
            post_event(f"http://127.0.0.1:{redirect.server_port}/events", "test-secret", b'{"id":"example"}')
        assert error.value.code == 302
        assert Sink.calls == 0
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


@pytest.mark.parametrize("primary_mode,secondary_mode,expected_status,expected_destination,expected_calls", [
    ("ok", "ok", "delivered", "primary", ["primary"]),
    ("unavailable", "ok", "delivered", "secondary", ["primary", "secondary"]),
    ("network", "ok", "delivered", "secondary", ["primary", "secondary"]),
    ("bad_request", "ok", "dead", None, ["primary"]),
    ("unavailable", "bad_request", "pending", None, ["primary", "secondary"]),
])
def test_regional_fallback_preserves_same_event_and_destination(app, monkeypatch, primary_mode, secondary_mode,
                                                               expected_status, expected_destination, expected_calls):
    import app.outbox_worker as worker

    primary = "https://primary.example/api/erp/events"
    secondary = "https://secondary.example/api/erp/events"
    app.config.update(ERP_AZURE_INGEST_URL=primary, ERP_AZURE_INGEST_FALLBACK_URL=secondary,
                      ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret", ERP_AZURE_INGEST_FALLBACK_HMAC_SECRET="test-secondary-secret")
    with app.app_context():
        event = enqueue_event("erp.test", "test", "regional", {"data": "same"})
        db.session.commit()
        event_id = event.id
    calls, bodies = [], []

    def delivery(url, secret, body):
        label = "primary" if url == primary else "secondary"
        assert secret == ("test-integration-secret" if label == "primary" else "test-secondary-secret")
        calls.append(label)
        bodies.append(body)
        mode = primary_mode if label == "primary" else secondary_mode
        if mode == "unavailable":
            raise HTTPError(url, 503, "Unavailable", {}, None)
        if mode == "bad_request":
            raise HTTPError(url, 400, "Bad request", {}, None)
        if mode == "network":
            raise URLError("network unavailable")

    monkeypatch.setattr(worker, "post_event", delivery)
    assert process_once(app, event_id)
    assert calls == expected_calls
    assert all(body == bodies[0] for body in bodies)
    with app.app_context():
        row = db.session.get(OutboxEvent, event_id)
        assert row.status == expected_status
        assert row.delivered_destination == expected_destination


@pytest.mark.parametrize("primary", ["http://primary.example/api/erp/events", "https://user:secret@primary.example/api/erp/events",
                                     "https://primary.example/api/erp/events?token=value", "https://primary.example/unexpected"])
def test_invalid_primary_without_fallback_stops_before_claiming_event(app, primary):
    app.config.update(ERP_AZURE_INGEST_URL=primary, ERP_AZURE_INGEST_FALLBACK_URL="",
                      ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret")
    with app.app_context():
        event = enqueue_event("erp.test", "test", "primary-invalid", {})
        db.session.commit()
        event_id = event.id
    with pytest.raises(RuntimeError):
        process_once(app)
    with app.app_context():
        event = db.session.get(OutboxEvent, event_id)
        assert event.status == "pending"
        assert event.attempts == 0


@pytest.mark.parametrize("fallback", ["http://secondary.example/api/erp/events", "https://user:secret@secondary.example/api/erp/events",
                                      "https://secondary.example/api/erp/events?token=value", "https://primary.example/api/erp/events",
                                      "https://PRIMARY.example:443/api/erp/events"])
def test_invalid_fallback_stops_before_claiming_event(app, fallback):
    app.config.update(ERP_AZURE_INGEST_URL="https://primary.example/api/erp/events", ERP_AZURE_INGEST_FALLBACK_URL=fallback,
                      ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret", ERP_AZURE_INGEST_FALLBACK_HMAC_SECRET="test-secondary-secret")
    with app.app_context():
        event = enqueue_event("erp.test", "test", "regional-invalid", {})
        db.session.commit()
        event_id = event.id
    with pytest.raises(RuntimeError):
        process_once(app, event_id)
    with app.app_context():
        row = db.session.get(OutboxEvent, event_id)
        assert row.status == "pending" and row.attempts == 0


def test_missing_secondary_secret_stops_before_claiming_event(app):
    app.config.update(ERP_AZURE_INGEST_URL="https://primary.example/api/erp/events",
                      ERP_AZURE_INGEST_FALLBACK_URL="https://secondary.example/api/erp/events",
                      ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret", ERP_AZURE_INGEST_FALLBACK_HMAC_SECRET="")
    with app.app_context():
        event = enqueue_event("erp.test", "test", "regional-secret", {})
        db.session.commit()
        event_id = event.id
    with pytest.raises(RuntimeError, match="secret is required"):
        process_once(app, event_id)
    with app.app_context():
        row = db.session.get(OutboxEvent, event_id)
        assert row.status == "pending" and row.attempts == 0


def test_regional_failure_honors_longest_retry_after(app, monkeypatch):
    import app.outbox_worker as worker

    app.config.update(ERP_AZURE_INGEST_URL="https://primary.example/api/erp/events",
                      ERP_AZURE_INGEST_FALLBACK_URL="https://secondary.example/api/erp/events",
                      ERP_AZURE_INGEST_HMAC_SECRET="test-integration-secret", ERP_AZURE_INGEST_FALLBACK_HMAC_SECRET="test-secondary-secret")
    with app.app_context():
        event = enqueue_event("erp.test", "test", "regional-throttled", {})
        db.session.commit()
        event_id = event.id

    def blocked(url, *_):
        seconds = "300" if "primary" in url else "600"
        raise HTTPError(url, 503, "Unavailable", {"Retry-After": seconds}, None)

    monkeypatch.setattr(worker, "post_event", blocked)
    assert process_once(app, event_id)
    with app.app_context():
        row = db.session.get(OutboxEvent, event_id)
        assert row.status == "pending"
        assert row.available_at >= utcnow_naive() + timedelta(seconds=599)
