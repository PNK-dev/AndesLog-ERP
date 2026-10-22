from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, UniqueConstraint
from werkzeug.security import check_password_hash, generate_password_hash


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="operator")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Warehouse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    city = db.Column(db.String(100), nullable=False, default="")
    active = db.Column(db.Boolean, nullable=False, default=True)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(160), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default="unidad")
    active = db.Column(db.Boolean, nullable=False, default=True)


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tax_id = db.Column(db.String(20), unique=True, nullable=False)
    business_name = db.Column(db.String(180), nullable=False)
    email = db.Column(db.String(180), nullable=False, default="")
    contact_name = db.Column(db.String(120), nullable=False, default="")
    active = db.Column(db.Boolean, nullable=False, default=True)


class InventoryBalance(db.Model):
    __tablename__ = "inventory_balances"
    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouse.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    quantity = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    warehouse = db.relationship(Warehouse)
    product = db.relationship(Product)
    __table_args__ = (UniqueConstraint("warehouse_id", "product_id"), CheckConstraint("quantity >= 0"))


class StockMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouse.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    movement_type = db.Column(db.String(12), nullable=False)
    quantity = db.Column(db.Numeric(12, 2), nullable=False)
    reason = db.Column(db.String(240), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    warehouse = db.relationship(Warehouse)
    product = db.relationship(Product)
    user = db.relationship(User)
    __table_args__ = (CheckConstraint("quantity > 0"), CheckConstraint("movement_type IN ('entrada', 'salida')"))


class SalesOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pendiente")
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    customer = db.relationship(Customer)
    created_by = db.relationship(User)
    lines = db.relationship("OrderLine", cascade="all, delete-orphan", lazy="selectin")
    __table_args__ = (CheckConstraint("status IN ('pendiente', 'preparando', 'despachado', 'entregado', 'cancelado')"),)


class OrderLine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("sales_order.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    quantity = db.Column(db.Numeric(12, 2), nullable=False)
    product = db.relationship(Product)
    __table_args__ = (CheckConstraint("quantity > 0"),)


class Vehicle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    plate = db.Column(db.String(20), unique=True, nullable=False)
    vehicle_type = db.Column(db.String(60), nullable=False)
    capacity_kg = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    active = db.Column(db.Boolean, nullable=False, default=True)


class Driver(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140), nullable=False)
    license_number = db.Column(db.String(50), unique=True, nullable=False)
    phone = db.Column(db.String(40), nullable=False, default="")
    active = db.Column(db.Boolean, nullable=False, default=True)


class Trip(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    origin = db.Column(db.String(120), nullable=False)
    destination = db.Column(db.String(120), nullable=False)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicle.id"), nullable=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("driver.id"), nullable=True)
    order_id = db.Column(db.Integer, db.ForeignKey("sales_order.id"), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="programado")
    departure_at = db.Column(db.DateTime, nullable=True)
    vehicle = db.relationship(Vehicle)
    driver = db.relationship(Driver)
    order = db.relationship(SalesOrder)
    __table_args__ = (CheckConstraint("status IN ('programado', 'en_ruta', 'completado', 'cancelado')"),)

