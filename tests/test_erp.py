import pytest

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


def test_inventory_receipt_and_prevent_negative_stock(app, client):
    login(client)
    with app.app_context():
        warehouse = Warehouse(code="LIM-01", name="Lima Norte", city="Lima")
        product = Product(sku="SKU-01", name="Caja logística", unit="caja")
        db.session.add_all([warehouse, product])
        db.session.commit()
        warehouse_id, product_id = warehouse.id, product.id

    receipt = client.post("/inventory", data={
        "warehouse_id": warehouse_id, "product_id": product_id,
        "movement_type": "entrada", "quantity": "12", "reason": "Recepción inicial",
    }, follow_redirects=True)
    assert b"Movimiento de inventario registrado" in receipt.data

    issue = client.post("/inventory", data={
        "warehouse_id": warehouse_id, "product_id": product_id,
        "movement_type": "salida", "quantity": "13", "reason": "Pedido",
    }, follow_redirects=True)
    assert b"Stock insuficiente" in issue.data
    with app.app_context():
        balance = InventoryBalance.query.one()
        assert str(balance.quantity) == "12.00"


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
