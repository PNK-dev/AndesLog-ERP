"""Concurrent claims against a disposable PostgreSQL server on loopback."""

import os
import re
import shutil
import socket
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from app import create_app
from app.models import OutboxEvent, db, utcnow_naive
from app.outbox import enqueue_event
from app.outbox_worker import process_once


def _bin(name):
    base = Path(os.environ.get("ANDESLOG_TEST_PG_BIN", r"C:\Program Files\PostgreSQL\18\bin"))
    if os.name != "nt" and not os.environ.get("ANDESLOG_TEST_PG_BIN"):
        found = shutil.which(name)
        return Path(found) if found else Path("/nonexistent")
    return base / (name + (".exe" if os.name == "nt" else ""))


@pytest.fixture
def postgres_app(tmp_path):
    if not all(_bin(name).is_file() for name in ("initdb", "pg_ctl")):
        pytest.skip("Local PostgreSQL server binaries are required")
    data = tmp_path / "pg-data"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    def run(name, *args, starting=False):
        result = subprocess.run([str(_bin(name)), *args],
                                stdout=subprocess.DEVNULL if starting else subprocess.PIPE,
                                stderr=subprocess.DEVNULL if starting else subprocess.PIPE,
                                text=True, timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError(f"Temporary {name} failed: {(result.stderr or '')[-1000:]}")

    run("initdb", "-D", str(data), "--username=postgres", "--auth=trust", "--no-locale", "--encoding=UTF8")
    run("pg_ctl", "start", "-D", str(data), "-l", str(tmp_path / "postgres.log"), "-w", "-t", "30",
        "-o", f"-h 127.0.0.1 -p {port}", starting=True)
    try:
        app = create_app({"TESTING": True, "SECRET_KEY": "local-test-only",
                          "SQLALCHEMY_DATABASE_URI": f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres",
                          "ERP_AZURE_INGEST_URL": "https://example.test/api/erp/events",
                          "ERP_AZURE_INGEST_HMAC_SECRET": "local-test-only"})
        with app.app_context():
            db.create_all()
        yield app
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
    finally:
        run("pg_ctl", "stop", "-D", str(data), "-m", "immediate", "-w", "-t", "30")


def test_expired_worker_cannot_revert_newer_delivery(postgres_app, monkeypatch):
    import app.outbox_worker as worker
    from urllib.error import HTTPError

    with postgres_app.app_context():
        event = enqueue_event("erp.test", "test", "concurrent", {})
        db.session.commit()
        event_id = event.id
    first_waiting, release_first = threading.Event(), threading.Event()
    exceptions = []

    def post(*_):
        if threading.current_thread().name == "old-worker":
            first_waiting.set()
            if not release_first.wait(10):
                raise RuntimeError("test worker timed out")
            raise HTTPError("https://example.test", 400, "Bad Request", {}, None)

    def run_old():
        try:
            process_once(postgres_app, event_id=event_id)
        except Exception as exc:
            exceptions.append(type(exc).__name__)

    monkeypatch.setattr(worker, "post_event", post)
    thread = threading.Thread(target=run_old, name="old-worker", daemon=True)
    thread.start()
    try:
        assert first_waiting.wait(10)
        with postgres_app.app_context():
            current = db.session.get(OutboxEvent, event_id)
            current.locked_until = utcnow_naive() - timedelta(seconds=1)
            db.session.commit()
        assert process_once(postgres_app, event_id=event_id) is True
    finally:
        release_first.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert exceptions == []
    with postgres_app.app_context():
        current = db.session.get(OutboxEvent, event_id)
        assert current.status == "delivered"
        assert current.attempts == 2


def test_session_and_csrf_survive_switch_to_another_node(postgres_app):
    from app.models import User, Warehouse

    config = {"TESTING": True, "SECRET_KEY": postgres_app.config["SECRET_KEY"],
              "SQLALCHEMY_DATABASE_URI": postgres_app.config["SQLALCHEMY_DATABASE_URI"],
              "SESSION_COOKIE_SECURE": False, "WTF_CSRF_ENABLED": True}
    node_b = create_app(config)
    wrong_key_node = create_app(dict(config, SECRET_KEY="different-key"))
    with postgres_app.app_context():
        user = User(username="switch-test", role="admin")
        user.set_password("test-password-only-2026")
        db.session.add(user)
        db.session.commit()
    client_a, client_b, wrong_client = postgres_app.test_client(), node_b.test_client(), wrong_key_node.test_client()
    def token(response):
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        assert match
        return match.group(1).decode()
    try:
        login_csrf = token(client_a.get("/login"))
        assert client_a.post("/login", data={"username": "switch-test", "password": "test-password-only-2026",
                                             "csrf_token": login_csrf}).status_code == 302
        form_csrf = token(client_a.get("/warehouses"))
        cookie = client_a.get_cookie("session")
        assert cookie is not None
        client_b.set_cookie("session", cookie.value, domain="localhost")
        wrong_client.set_cookie("session", cookie.value, domain="localhost")
        assert wrong_client.get("/warehouses").status_code == 302
        response = client_b.post("/warehouses", data={"code": "SWITCH", "name": "Nodo alternativo",
                                                     "city": "Lima", "csrf_token": form_csrf})
        assert response.status_code == 302
        with postgres_app.app_context():
            assert Warehouse.query.filter_by(code="SWITCH").one().name == "Nodo alternativo"
        assert b"Nodo alternativo" in client_a.get("/warehouses").data
    finally:
        for instance in (node_b, wrong_key_node):
            with instance.app_context():
                db.session.remove()
                db.engine.dispose()


def test_concurrent_inventory_retries_do_not_duplicate_business_or_outbox(postgres_app):
    """Two ERP nodes use PostgreSQL locks and a unique key for identical retries."""
    from uuid import uuid4
    from app.models import InventoryBalance, Product, StockMovement, User, Warehouse

    config = {"TESTING": True, "SECRET_KEY": postgres_app.config["SECRET_KEY"],
              "SQLALCHEMY_DATABASE_URI": postgres_app.config["SQLALCHEMY_DATABASE_URI"],
              "SESSION_COOKIE_SECURE": False, "WTF_CSRF_ENABLED": False}
    nodes = [create_app(config), create_app(config)]
    with postgres_app.app_context():
        user = User(username="concurrent-inventory", role="admin")
        user.set_password("local-concurrent-password")
        warehouse = Warehouse(code="CONCURRENT", name="Local", city="Lima")
        product = Product(sku="CONCURRENT", name="Local", unit="unidad")
        db.session.add_all([user, warehouse, product])
        db.session.commit()
        warehouse_id, product_id = warehouse.id, product.id
    shared_key, other_key = str(uuid4()), str(uuid4())
    start = threading.Barrier(4, timeout=10)
    results, errors = [], []

    def send(index):
        try:
            client = nodes[index % 2].test_client()
            assert client.post("/login", data={"username": "concurrent-inventory",
                "password": "local-concurrent-password"}).status_code == 302
            start.wait()
            result = client.post("/inventory", data={"operation_id": shared_key if index < 3 else other_key,
                "warehouse_id": warehouse_id, "product_id": product_id, "movement_type": "entrada",
                "quantity": "3" if index < 3 else "5", "reason": "concurrent test"})
            results.append(result.status_code)
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=send, args=(i,), daemon=True) for i in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert results == [302] * 4
        with postgres_app.app_context():
            assert InventoryBalance.query.one().quantity == 8
            assert StockMovement.query.count() == 2
            assert OutboxEvent.query.filter_by(event_type="erp.inventory.movement_recorded").count() == 2
    finally:
        for instance in nodes:
            with instance.app_context():
                db.session.remove()
                db.engine.dispose()


def test_synthetic_erp_dump_restores_with_migrations_and_detects_corruption(postgres_app, tmp_path):
    """Restore only into new databases on the disposable loopback cluster."""
    import psycopg
    import importlib.util
    import time
    from flask_migrate import upgrade
    from sqlalchemy import text
    from app.models import Customer, InventoryBalance, Product, SalesOrder, User, Warehouse

    if not all(_bin(name).is_file() for name in ("pg_dump", "pg_restore")):
        pytest.skip("PostgreSQL dump/restore binaries are required")
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with postgres_app.app_context():
        # Use the versioned migrations, not a synthetic create_all schema.
        db.drop_all()
        upgrade(directory=str(migrations))
        user = User(username="restore-tester", role="admin")
        user.set_password("Local-test-password-2026!")
        customer = Customer(tax_id="20100000001", business_name="Synthetic customer")
        warehouse = Warehouse(code="LAB-01", name="Synthetic warehouse", city="Lima")
        product = Product(sku="LAB-ITEM", name="Synthetic item")
        db.session.add_all([user, customer, warehouse, product])
        db.session.flush()
        db.session.add_all([
            InventoryBalance(warehouse_id=warehouse.id, product_id=product.id, quantity=12),
            SalesOrder(number="LAB-ORDER", customer_id=customer.id, created_by_id=user.id),
        ])
        event = enqueue_event("erp.test", "test", "restore", {"demo": True})
        db.session.commit()
        event_id = event.id
        url = db.engine.url
        migration_revision = db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    # A sibling Terraform repository may be named either as the local lab or
    # the published clone. This comparison reads only the disposable cluster.
    sibling_root = Path(__file__).resolve().parents[2]
    verifier_path = next((candidate for candidate in (
        sibling_root / "Terraform" / "scripts" / "verify_erp_restore.py",
        sibling_root / "AndesLog-Terraform" / "scripts" / "verify_erp_restore.py",
    ) if candidate.is_file()), None)
    verifier = None
    if verifier_path is not None:
        spec = importlib.util.spec_from_file_location("verify_erp_restore", verifier_path)
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        with psycopg.connect(host="127.0.0.1", port=url.port, user="postgres", dbname="postgres", sslmode="disable") as source:
            reference = verifier.snapshot(source, expected_host="127.0.0.1", max_rows=1000, deadline=time.monotonic() + 20)
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                source.execute("DELETE FROM public.warehouse")

    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
    env.update(PGHOST="127.0.0.1", PGPORT=str(url.port), PGUSER="postgres", PGSSLMODE="disable")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def command(args):
        return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60, creationflags=flags)

    archive = tmp_path / "synthetic-erp.dump"
    dumped = command([str(_bin("pg_dump")), "--format=custom", "--no-owner", "--file", str(archive), "postgres"])
    assert dumped.returncode == 0, dumped.stderr
    assert archive.stat().st_size > 0
    with psycopg.connect(host="127.0.0.1", port=url.port, user="postgres", dbname="postgres", autocommit=True) as admin:
        admin.execute("CREATE DATABASE andeslog_restore")
        admin.execute("CREATE DATABASE andeslog_corrupt_restore")
    restored = command([str(_bin("pg_restore")), "--no-owner", "--no-acl", "--single-transaction",
                        "--dbname", "andeslog_restore", str(archive)])
    assert restored.returncode == 0, restored.stderr

    restore_app = create_app({"TESTING": True, "SECRET_KEY": "local-test-only", "WTF_CSRF_ENABLED": False,
                              "SQLALCHEMY_DATABASE_URI": str(url.set(database="andeslog_restore"))})
    try:
        with restore_app.app_context():
            upgrade(directory=str(migrations))
            assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == migration_revision
            assert Warehouse.query.filter_by(code="LAB-01").one().name == "Synthetic warehouse"
            assert InventoryBalance.query.one().quantity == 12
            assert SalesOrder.query.filter_by(number="LAB-ORDER").one().status == "pendiente"
            assert db.session.get(OutboxEvent, event_id).payload == {"demo": True}
        client = restore_app.test_client()
        assert client.get("/api/health/ready").status_code == 200
        assert client.post("/login", data={"username": "restore-tester", "password": "Local-test-password-2026!"}).status_code == 302
        assert client.get("/").status_code == 200
        if verifier is not None:
            with psycopg.connect(host="127.0.0.1", port=url.port, user="postgres", dbname="andeslog_restore", sslmode="disable") as target:
                restored_snapshot = verifier.snapshot(target, expected_host="127.0.0.1", max_rows=1000, deadline=time.monotonic() + 20)
            assert verifier.compare(reference, restored_snapshot)["matching"]
            with psycopg.connect(host="127.0.0.1", port=url.port, user="postgres", dbname="andeslog_restore", autocommit=True) as target:
                target.execute("UPDATE public.inventory_balances SET quantity = quantity + 1")
            with psycopg.connect(host="127.0.0.1", port=url.port, user="postgres", dbname="andeslog_restore", sslmode="disable") as target:
                changed_snapshot = verifier.snapshot(target, expected_host="127.0.0.1", max_rows=1000, deadline=time.monotonic() + 20)
            assert verifier.compare(reference, changed_snapshot)["different_tables"] == ["inventory"]
    finally:
        with restore_app.app_context():
            db.session.remove()
            db.engine.dispose()

    corrupted = tmp_path / "truncated.dump"
    raw = archive.read_bytes()
    corrupted.write_bytes(raw[: len(raw) // 2])
    failed = command([str(_bin("pg_restore")), "--no-owner", "--no-acl", "--single-transaction",
                      "--dbname", "andeslog_corrupt_restore", str(corrupted)])
    assert failed.returncode != 0
    with psycopg.connect(host="127.0.0.1", port=url.port, user="postgres", dbname="andeslog_corrupt_restore", autocommit=True) as target:
        assert target.execute("SELECT to_regclass('public.warehouse')").fetchone()[0] is None


def test_migration_downgrade_refuses_to_forget_secondary_archive(postgres_app):
    from flask_migrate import downgrade, upgrade

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with postgres_app.app_context():
        db.drop_all()
        upgrade(directory=str(migrations))
        event = enqueue_event("erp.test", "test", "migration", {})
        event.status = "delivered"
        event.delivered_destination = "secondary"
        db.session.commit()
        with pytest.raises(SystemExit) as exit_info:
            downgrade(directory=str(migrations), revision="c81e4da50732")
        assert exit_info.value.code == 1
        db.session.rollback()
        assert db.session.get(OutboxEvent, event.id).delivered_destination == "secondary"
