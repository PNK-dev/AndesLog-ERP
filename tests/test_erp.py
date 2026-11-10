import pytest
from uuid import uuid4
from sqlalchemy.exc import OperationalError

from app import create_app
from app.models import Customer, Driver, InventoryBalance, Product, SalesOrder, Trip, User, Vehicle, Warehouse, db


@pytest.fixture()
def app(tmp_path):
    instance = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret-key",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
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


def login(client):
    return client.post("/login", data={"username": "admin", "password": "Test-password-2026!"}, follow_redirects=True)


def test_login_required_and_valid_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    response = login(client)
    assert response.status_code == 200
    assert b"CENTRO DE OPERACIONES" in response.data


def test_security_headers_are_present(client):
    response = client.get("/login")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_health_live_does_not_depend_on_database(client, app, monkeypatch):
    with app.app_context():
        def database_unavailable(*args, **kwargs):
            raise OperationalError("SELECT 1", {}, RuntimeError("database unavailable"))

        monkeypatch.setattr(db.session, "execute", database_unavailable)
        response = client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json["status"] == "alive"


def test_health_ready_checks_database(client):
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json == {
        "status": "ready",
        "service": "andeslog-erp",
        "database": "ok",
    }


def test_health_ready_reports_database_failure(client, app, monkeypatch):
    with app.app_context():
        def database_unavailable(*args, **kwargs):
            raise OperationalError("SELECT 1", {}, RuntimeError("database unavailable"))

        monkeypatch.setattr(db.session, "execute", database_unavailable)
        response = client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.json["status"] == "not_ready"
    assert response.json["database"] == "unavailable"


def test_inventory_receipt_and_prevent_negative_stock(app, client):
    login(client)
    with app.app_context():
        warehouse = Warehouse(code="LIM-01", name="Lima Norte", city="Lima")
        product = Product(sku="SKU-01", name="Caja logística", unit="caja")
        db.session.add_all([warehouse, product])
        db.session.commit()
        warehouse_id, product_id = warehouse.id, product.id

    receipt = client.post("/inventory", data={
        "operation_id": str(uuid4()),
        "warehouse_id": warehouse_id, "product_id": product_id,
        "movement_type": "entrada", "quantity": "12", "reason": "Recepción inicial",
    }, follow_redirects=True)
    assert b"Movimiento de inventario registrado" in receipt.data

    issue = client.post("/inventory", data={
        "operation_id": str(uuid4()),
        "warehouse_id": warehouse_id, "product_id": product_id,
        "movement_type": "salida", "quantity": "13", "reason": "Pedido",
    }, follow_redirects=True)
    assert b"Stock insuficiente" in issue.data
    with app.app_context():
        balance = InventoryBalance.query.one()
        assert str(balance.quantity) == "12.00"


def test_inventory_retry_is_idempotent_and_rejects_different_payload(app, client):
    from app.models import OutboxEvent, StockMovement

    login(client)
    with app.app_context():
        warehouse = Warehouse(code="IDEMP", name="Almacén", city="Lima")
        product = Product(sku="IDEMP-01", name="Caja", unit="unidad")
        db.session.add_all([warehouse, product])
        db.session.commit()
        warehouse_id, product_id = warehouse.id, product.id
    operation = str(uuid4())
    data = {"operation_id": operation, "warehouse_id": warehouse_id, "product_id": product_id,
            "movement_type": "entrada", "quantity": "3", "reason": "Recepción"}
    assert client.post("/inventory", data=data).status_code == 302
    assert client.post("/inventory", data=data).status_code == 302
    assert client.post("/inventory", data=dict(data, quantity="4")).status_code == 409
    assert client.post("/inventory", data=dict(data, operation_id="bad")).status_code == 400
    with app.app_context():
        assert InventoryBalance.query.one().quantity == 3
        assert StockMovement.query.one().operation_id == operation
        event = OutboxEvent.query.filter_by(event_type="erp.inventory.movement_recorded").one()
        assert event.payload["operation_id"] == operation
        assert event.id != operation


def test_order_and_transport_lifecycle(app, client):
    login(client)
    with app.app_context():
        customer = Customer(tax_id="20123456789", business_name="Cliente Demo S.A.C.")
        product = Product(sku="SKU-02", name="Pallet industrial", unit="pallet")
        vehicle = Vehicle(plate="ABC-123", vehicle_type="Camión", capacity_kg=5000)
        driver = Driver(name="Ana Chofer", license_number="Q12345678")
        db.session.add_all([customer, product, vehicle, driver])
        db.session.commit()
        values = (customer.id, product.id, vehicle.id, driver.id)

    order_response = client.post("/orders", data={
        "number": "PED-001", "customer_id": values[0], "product_id": values[1], "quantity": "4",
    }, follow_redirects=True)
    assert b"Pedido PED-001 registrado" in order_response.data
    with app.app_context():
        order_id = SalesOrder.query.filter_by(number="PED-001").one().id

    trip_response = client.post("/transport", data={
        "action": "trip", "trip_code": "VIA-001", "origin": "Lima", "destination": "Arequipa",
        "vehicle_id": values[2], "driver_id": values[3], "order_id": order_id,
    }, follow_redirects=True)
    assert b"Viaje programado" in trip_response.data

    route_response = client.post("/transport/1/status", data={"status": "en_ruta"}, follow_redirects=True)
    assert route_response.status_code == 200
    assert b"Viaje VIA-001: en ruta" in route_response.data
    with app.app_context():
        trip = db.session.get(Trip, 1)
        assert trip.status == "en_ruta"
        assert trip.departure_at is not None

    complete_response = client.post("/transport/1/status", data={"status": "completado"}, follow_redirects=True)
    assert b"Viaje VIA-001: completado" in complete_response.data
    with app.app_context():
        assert SalesOrder.query.filter_by(number="PED-001").one().status == "entregado"


def test_operator_cannot_manage_users(app, client):
    with app.app_context():
        operator = User(username="operador", role="operator")
        operator.set_password("Test-password-operator!")
        db.session.add(operator)
        db.session.commit()
    client.post("/login", data={"username": "operador", "password": "Test-password-operator!"})
    assert client.get("/users").status_code == 403




def test_all_erp_pages_render_for_admin(client):
    login(client)
    for path in ("/warehouses", "/products", "/customers", "/inventory", "/orders", "/transport", "/users"):
        response = client.get(path)
        assert response.status_code == 200, path
