from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .models import (
    Customer, Driver, InventoryBalance, OrderLine, Product, SalesOrder,
    StockMovement, Trip, User, Vehicle, Warehouse, db,
)

bp = Blueprint("main", __name__)
ORDER_STATUSES = ["pendiente", "preparando", "despachado", "entregado", "cancelado"]
TRIP_STATUSES = ["programado", "en_ruta", "completado", "cancelado"]


def positive_decimal(raw: str | None) -> Decimal:
    try:
        value = Decimal(raw or "")
    except (InvalidOperation, TypeError):
        raise ValueError("Ingresa una cantidad numérica válida.")
    if not value.is_finite() or value <= 0:
        raise ValueError("La cantidad debe ser mayor que cero.")
    return value.quantize(Decimal("0.01"))


def create_unique(model, **values):
    item = model(**values)
    db.session.add(item)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ValueError("Ya existe un registro con ese código o identificador.")
    return item


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user, remember=False, fresh=True)
            next_url = request.args.get("next", "")
            parts = urlsplit(next_url)
            if next_url.startswith("/") and not parts.netloc and not parts.scheme:
                return redirect(next_url)
            return redirect(url_for("main.dashboard"))
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "success")
    return redirect(url_for("main.login"))


@bp.get("/")
@login_required
def dashboard():
    low_stock = InventoryBalance.query.filter(InventoryBalance.quantity <= 5).count()
    metrics = {
        "warehouses": Warehouse.query.filter_by(active=True).count(),
        "products": Product.query.filter_by(active=True).count(),
        "customers": Customer.query.filter_by(active=True).count(),
        "orders": SalesOrder.query.filter(SalesOrder.status.in_(["pendiente", "preparando"])).count(),
        "trips": Trip.query.filter(Trip.status.in_(["programado", "en_ruta"])).count(),
        "low_stock": low_stock,
    }
    recent_orders = SalesOrder.query.order_by(SalesOrder.created_at.desc()).limit(6).all()
    return render_template("dashboard.html", metrics=metrics, recent_orders=recent_orders)


@bp.route("/warehouses", methods=["GET", "POST"])
@login_required
def warehouses():
    if request.method == "POST":
        code, name = request.form.get("code", "").strip().upper(), request.form.get("name", "").strip()
        if not code or not name:
            flash("Código y nombre del almacén son obligatorios.", "error")
        else:
            try:
                create_unique(Warehouse, code=code, name=name, city=request.form.get("city", "").strip())
                flash("Almacén creado.", "success")
                return redirect(url_for("main.warehouses"))
            except ValueError as exc:
                flash(str(exc), "error")
    items = Warehouse.query.order_by(Warehouse.name).all()
    return render_template("warehouses.html", items=items)


@bp.route("/products", methods=["GET", "POST"])
@login_required
def products():
    if request.method == "POST":
        sku, name = request.form.get("sku", "").strip().upper(), request.form.get("name", "").strip()
        if not sku or not name:
            flash("SKU y nombre son obligatorios.", "error")
        else:
            try:
                create_unique(Product, sku=sku, name=name, unit=request.form.get("unit", "unidad").strip() or "unidad")
                flash("Producto creado.", "success")
                return redirect(url_for("main.products"))
            except ValueError as exc:
                flash(str(exc), "error")
    items = Product.query.order_by(Product.name).all()
    return render_template("products.html", items=items)


@bp.route("/customers", methods=["GET", "POST"])
@login_required
def customers():
    if request.method == "POST":
        tax_id = request.form.get("tax_id", "").strip()
        business_name = request.form.get("business_name", "").strip()
        if not tax_id or not business_name:
            flash("RUC/ID fiscal y razón social son obligatorios.", "error")
        else:
            try:
                create_unique(Customer, tax_id=tax_id, business_name=business_name,
                              email=request.form.get("email", "").strip(),
                              contact_name=request.form.get("contact_name", "").strip())
                flash("Cliente creado.", "success")
                return redirect(url_for("main.customers"))
            except ValueError as exc:
                flash(str(exc), "error")
    items = Customer.query.order_by(Customer.business_name).all()
    return render_template("customers.html", items=items)


@bp.route("/inventory", methods=["GET", "POST"])
@login_required
def inventory():
    if request.method == "POST":
        try:
            warehouse = db.session.get(Warehouse, int(request.form.get("warehouse_id", "0")))
            product = db.session.get(Product, int(request.form.get("product_id", "0")))
            movement_type = request.form.get("movement_type", "")
            quantity = positive_decimal(request.form.get("quantity"))
            if not warehouse or not product or movement_type not in {"entrada", "salida"}:
                raise ValueError("Selecciona un almacén, producto y tipo de movimiento válidos.")
            balance = InventoryBalance.query.filter_by(warehouse_id=warehouse.id, product_id=product.id).with_for_update().first()
            if not balance:
                balance = InventoryBalance(warehouse=warehouse, product=product, quantity=Decimal("0"))
                db.session.add(balance)
            if movement_type == "salida" and balance.quantity < quantity:
                raise ValueError(f"Stock insuficiente. Disponible: {balance.quantity} {product.unit}.")
            balance.quantity += quantity if movement_type == "entrada" else -quantity
            db.session.add(StockMovement(warehouse=warehouse, product=product, user=current_user,
                                         movement_type=movement_type, quantity=quantity,
                                         reason=request.form.get("reason", "").strip()))
            db.session.commit()
            flash("Movimiento de inventario registrado.", "success")
            return redirect(url_for("main.inventory"))
        except (ValueError, TypeError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
    balances = InventoryBalance.query.join(Warehouse).join(Product).order_by(Warehouse.name, Product.name).all()
    movements = StockMovement.query.order_by(StockMovement.created_at.desc()).limit(12).all()
    return render_template("inventory.html", balances=balances, movements=movements,
                           warehouses=Warehouse.query.filter_by(active=True).order_by(Warehouse.name).all(),
                           products=Product.query.filter_by(active=True).order_by(Product.name).all())


@bp.route("/orders", methods=["GET", "POST"])
@login_required
def orders():
    if request.method == "POST":
        try:
            customer = db.session.get(Customer, int(request.form.get("customer_id", "0")))
            product = db.session.get(Product, int(request.form.get("product_id", "0")))
            quantity = positive_decimal(request.form.get("quantity"))
            if not customer or not product:
                raise ValueError("Selecciona un cliente y producto válidos.")
            order = SalesOrder(number=request.form.get("number", "").strip().upper(),
                               customer=customer, created_by=current_user, status="pendiente")
            if not order.number:
                raise ValueError("El número de pedido es obligatorio.")
            order.lines.append(OrderLine(product=product, quantity=quantity))
            db.session.add(order)
            db.session.commit()
            flash(f"Pedido {order.number} registrado.", "success")
            return redirect(url_for("main.orders"))
        except (ValueError, TypeError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
        except IntegrityError:
            db.session.rollback()
            flash("Ese número de pedido ya existe.", "error")
    items = SalesOrder.query.order_by(SalesOrder.created_at.desc()).all()
    return render_template("orders.html", items=items, statuses=ORDER_STATUSES,
                           customers=Customer.query.filter_by(active=True).order_by(Customer.business_name).all(),
                           products=Product.query.filter_by(active=True).order_by(Product.name).all())


@bp.post("/orders/<int:order_id>/status")
@login_required
def order_status(order_id: int):
    order = db.get_or_404(SalesOrder, order_id)
    status = request.form.get("status", "")
    if status not in ORDER_STATUSES:
        abort(400)
    order.status = status
    db.session.commit()
    flash(f"Pedido {order.number}: {status.replace('_', ' ')}.", "success")
    return redirect(url_for("main.orders"))


@bp.route("/transport", methods=["GET", "POST"])
@login_required
def transport():
    if request.method == "POST":
        action = request.form.get("action", "")
        try:
            if action == "vehicle":
                plate = request.form.get("plate", "").strip().upper()
                vehicle_type = request.form.get("vehicle_type", "").strip()
                if not plate or not vehicle_type:
                    raise ValueError("Placa y tipo de vehículo son obligatorios.")
                capacity = positive_decimal(request.form.get("capacity_kg"))
                create_unique(Vehicle, plate=plate, vehicle_type=vehicle_type, capacity_kg=capacity)
                flash("Vehículo registrado.", "success")
            elif action == "driver":
                name = request.form.get("driver_name", "").strip()
                license_number = request.form.get("license_number", "").strip().upper()
                if not name or not license_number:
                    raise ValueError("Nombre y licencia del conductor son obligatorios.")
                create_unique(Driver, name=name, license_number=license_number, phone=request.form.get("phone", "").strip())
                flash("Conductor registrado.", "success")
            elif action == "trip":
                code = request.form.get("trip_code", "").strip().upper()
                origin = request.form.get("origin", "").strip()
                destination = request.form.get("destination", "").strip()
                if not all((code, origin, destination)):
                    raise ValueError("Código, origen y destino son obligatorios para programar un viaje.")
                vehicle_id = request.form.get("vehicle_id") or None
                driver_id = request.form.get("driver_id") or None
                order_id = request.form.get("order_id") or None
                trip = Trip(code=code, origin=origin, destination=destination,
                            vehicle_id=int(vehicle_id) if vehicle_id else None,
                            driver_id=int(driver_id) if driver_id else None,
                            order_id=int(order_id) if order_id else None)
                db.session.add(trip)
                db.session.commit()
                flash("Viaje programado.", "success")
            else:
                raise ValueError("Acción de transporte no reconocida.")
            return redirect(url_for("main.transport"))
        except (ValueError, TypeError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
        except IntegrityError:
            db.session.rollback()
            flash("El código o identificador ya está registrado.", "error")
    return render_template("transport.html",
                           trips=Trip.query.order_by(Trip.id.desc()).all(),
                           vehicles=Vehicle.query.filter_by(active=True).order_by(Vehicle.plate).all(),
                           drivers=Driver.query.filter_by(active=True).order_by(Driver.name).all(),
                           orders=SalesOrder.query.filter(SalesOrder.status.notin_(["entregado", "cancelado"])).order_by(SalesOrder.id.desc()).all(),
                           all_vehicles=Vehicle.query.order_by(Vehicle.plate).all(),
                           all_drivers=Driver.query.order_by(Driver.name).all(), statuses=TRIP_STATUSES)


@bp.post("/transport/<int:trip_id>/status")
@login_required
def trip_status(trip_id: int):
    trip = db.get_or_404(Trip, trip_id)
    status = request.form.get("status", "")
    if status not in TRIP_STATUSES:
        abort(400)
    trip.status = status
    if status == "en_ruta" and not trip.departure_at:
        trip.departure_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if status == "completado" and trip.order and trip.order.status != "cancelado":
        trip.order.status = "entregado"
    db.session.commit()
    flash(f"Viaje {trip.code}: {status.replace('_', ' ')}.", "success")
    return redirect(url_for("main.transport"))


@bp.route("/users", methods=["GET", "POST"])
@login_required
def users():
    if current_user.role != "admin":
        abort(403)
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "operator")
        if not username or len(password) < 12 or role not in {"admin", "operator"}:
            flash("Indica usuario, contraseña de al menos 12 caracteres y un rol válido.", "error")
        elif User.query.filter_by(username=username).first():
            flash("Ese usuario ya existe.", "error")
        else:
            user = User(username=username, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("Usuario creado.", "success")
            return redirect(url_for("main.users"))
    return render_template("users.html", items=User.query.order_by(User.username).all())


@bp.get("/api/health")
def api_health():
    return jsonify(status="ok", service="andeslog-erp")


@bp.get("/api/v1/summary")
@login_required
def api_summary():
    return jsonify(warehouses=Warehouse.query.count(), products=Product.query.count(),
                   customers=Customer.query.count(), open_orders=SalesOrder.query.filter(
                       SalesOrder.status.in_(["pendiente", "preparando"])).count(), active_trips=Trip.query.filter(
                       Trip.status.in_(["programado", "en_ruta"])).count())


