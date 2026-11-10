from __future__ import annotations

import csv
import io
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import tuple_
from sqlalchemy.exc import IntegrityError

from .models import (
    Customer,
    Driver,
    InventoryBalance,
    OrderLine,
    Product,
    SalesOrder,
    StockMovement,
    Trip,
    Vehicle,
    Warehouse,
    db,
    utcnow_naive,
)
from .outbox import enqueue_event

CSV_COLUMNS = (
    "tipo",
    "codigo",
    "nombre",
    "ciudad",
    "email",
    "contacto",
    "unidad",
    "almacen",
    "producto",
    "cantidad",
    "capacidad_kg",
    "telefono",
    "cliente",
    "estado",
    "origen",
    "destino",
    "vehiculo",
    "conductor",
    "pedido",
)
MAX_IMPORT_ROWS = 500
ORDER_STATUSES = {"pendiente", "preparando", "despachado", "entregado", "cancelado"}
TRIP_STATUSES = {"programado", "en_ruta", "completado", "cancelado"}


class CsvImportError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("El CSV tiene errores de validación.")


def _ascii_lower(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value.lower())
        if not unicodedata.combining(char)
    )


def _required(row: dict[str, str], key: str, label: str, max_length: int) -> str:
    value = (row.get(key) or "").strip()
    if not value:
        raise ValueError(f"falta {label}")
    if len(value) > max_length:
        raise ValueError(f"{label} supera {max_length} caracteres")
    return value


def _optional(row: dict[str, str], key: str, label: str, max_length: int) -> str:
    value = (row.get(key) or "").strip()
    if len(value) > max_length:
        raise ValueError(f"{label} supera {max_length} caracteres")
    return value


def _positive_decimal(raw: str, label: str) -> Decimal:
    value = (raw or "").strip()
    if "," in value and "." not in value:
        value = value.replace(",", ".")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError(f"{label} debe ser numérico")
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{label} debe ser mayor que cero")
    try:
        rounded = number.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(f"{label} tiene demasiados decimales o dígitos")
    if rounded <= 0:
        raise ValueError(f"{label} debe ser al menos 0.01")
    if rounded >= Decimal("10000000000"):
        raise ValueError(f"{label} excede el máximo permitido")
    return rounded


def _csv_reader(text: str) -> csv.DictReader:
    sample = text[:8192]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;").delimiter
    except csv.Error:
        delimiter = ";" if sample.splitlines() and sample.splitlines()[0].count(";") > sample.splitlines()[0].count(",") else ","
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    if not reader.fieldnames:
        raise CsvImportError(["El archivo está vacío o no contiene una fila de encabezados."])
    headers = [(_ascii_lower(name or "").strip()) for name in reader.fieldnames]
    if len(headers) != len(set(headers)):
        raise CsvImportError(["El CSV contiene encabezados duplicados."])
    missing = [column for column in CSV_COLUMNS if column not in headers]
    unknown = [column for column in headers if column not in CSV_COLUMNS]
    if missing or unknown:
        details = []
        if missing:
            details.append("faltan columnas: " + ", ".join(missing))
        if unknown:
            details.append("columnas no reconocidas: " + ", ".join(unknown))
        raise CsvImportError(["Encabezados inválidos; " + "; ".join(details) + "."])
    reader.fieldnames = headers
    return reader


def _load_map(model, key_name: str, keys: set[str]) -> dict[str, object]:
    if not keys:
        return {}
    key_column = getattr(model, key_name)
    rows = db.session.query(model).filter(key_column.in_(keys)).all()
    return {getattr(item, key_name): item for item in rows}


def import_logistics_csv(text: str, user_id: int) -> dict[str, int]:
    """Validate and atomically import a mixed logistics CSV."""
    reader = _csv_reader(text)
    staged: dict[str, list[dict]] = defaultdict(list)
    errors: list[str] = []
    seen: dict[str, set[object]] = defaultdict(set)
    row_count = 0

    for line_number, raw in enumerate(reader, start=2):
        if raw is None:
            continue
        if None in raw:
            errors.append(f"Fila {line_number}: tiene más valores que columnas.")
            continue
        row = {key: (value or "").strip() for key, value in raw.items()}
        if not any(row.values()):
            continue
        row_count += 1
        if row_count > MAX_IMPORT_ROWS:
            raise CsvImportError([f"El archivo supera el máximo de {MAX_IMPORT_ROWS} filas de datos."])

        kind = _ascii_lower(row.get("tipo", "").strip())
        try:
            if kind == "almacen":
                item = {
                    "code": _required(row, "codigo", "código de almacén", 30).upper(),
                    "name": _required(row, "nombre", "nombre de almacén", 120),
                    "city": _optional(row, "ciudad", "ciudad", 100),
                }
                key = item["code"]
            elif kind == "producto":
                item = {
                    "sku": _required(row, "codigo", "SKU", 50).upper(),
                    "name": _required(row, "nombre", "nombre de producto", 160),
                    "unit": _optional(row, "unidad", "unidad", 20) or "unidad",
                }
                key = item["sku"]
            elif kind == "cliente":
                item = {
                    "tax_id": _required(row, "codigo", "RUC/ID fiscal", 20),
                    "business_name": _required(row, "nombre", "razón social", 180),
                    "email": _optional(row, "email", "email", 180),
                    "contact_name": _optional(row, "contacto", "contacto", 120),
                }
                if item["email"] and ("@" not in item["email"] or item["email"].startswith("@") or item["email"].endswith("@")):
                    raise ValueError("email no tiene un formato válido")
                key = item["tax_id"]
            elif kind == "inventario":
                item = {
                    "warehouse_code": _required(row, "almacen", "código de almacén", 30).upper(),
                    "sku": _required(row, "producto", "SKU", 50).upper(),
                    "quantity": _positive_decimal(row.get("cantidad", ""), "cantidad inicial"),
                }
                key = (item["warehouse_code"], item["sku"])
            elif kind == "vehiculo":
                item = {
                    "plate": _required(row, "codigo", "placa", 20).upper(),
                    "vehicle_type": _required(row, "nombre", "tipo de vehículo", 60),
                    "capacity_kg": _positive_decimal(row.get("capacidad_kg", ""), "capacidad_kg"),
                }
                key = item["plate"]
            elif kind == "conductor":
                item = {
                    "license_number": _required(row, "codigo", "número de licencia", 50).upper(),
                    "name": _required(row, "nombre", "nombre de conductor", 140),
                    "phone": _optional(row, "telefono", "teléfono", 40),
                }
                key = item["license_number"]
            elif kind == "pedido":
                status = _ascii_lower(_optional(row, "estado", "estado", 20) or "pendiente").replace(" ", "_")
                if status not in ORDER_STATUSES:
                    raise ValueError("estado de pedido no válido")
                item = {
                    "number": _required(row, "codigo", "número de pedido", 40).upper(),
                    "customer_tax_id": _required(row, "cliente", "RUC/ID del cliente", 20),
                    "sku": _required(row, "producto", "SKU", 50).upper(),
                    "quantity": _positive_decimal(row.get("cantidad", ""), "cantidad del pedido"),
                    "status": status,
                }
                key = item["number"]
            elif kind == "viaje":
                status = _ascii_lower(_optional(row, "estado", "estado", 20) or "programado").replace(" ", "_")
                if status not in TRIP_STATUSES:
                    raise ValueError("estado de viaje no válido")
                item = {
                    "code": _required(row, "codigo", "código de viaje", 40).upper(),
                    "origin": _required(row, "origen", "origen", 120),
                    "destination": _required(row, "destino", "destino", 120),
                    "plate": _optional(row, "vehiculo", "placa del vehículo", 20).upper(),
                    "license_number": _optional(row, "conductor", "licencia del conductor", 50).upper(),
                    "order_number": _optional(row, "pedido", "número de pedido", 40).upper(),
                    "status": status,
                }
                key = item["code"]
            else:
                raise ValueError("tipo no reconocido; usa almacen, producto, cliente, inventario, vehiculo, conductor, pedido o viaje")

            if key in seen[kind]:
                raise ValueError("identificador repetido dentro del CSV")
            seen[kind].add(key)
            item["line"] = line_number
            staged[kind].append(item)
        except ValueError as exc:
            errors.append(f"Fila {line_number}: {exc}.")

    if row_count == 0 and not errors:
        errors.append("El CSV contiene encabezados, pero no tiene filas de datos.")
    if errors:
        raise CsvImportError(errors[:50])

    warehouse_map = _load_map(Warehouse, "code", {item["code"] for item in staged["almacen"]})
    product_map = _load_map(Product, "sku", {item["sku"] for item in staged["producto"]})
    customer_map = _load_map(Customer, "tax_id", {item["tax_id"] for item in staged["cliente"]})
    vehicle_map = _load_map(Vehicle, "plate", {item["plate"] for item in staged["vehiculo"]})
    driver_map = _load_map(Driver, "license_number", {item["license_number"] for item in staged["conductor"]})
    order_map = _load_map(SalesOrder, "number", {item["number"] for item in staged["pedido"]})
    trip_map = _load_map(Trip, "code", {item["code"] for item in staged["viaje"]})

    entity_data = (
        ("almacen", staged["almacen"], "code", warehouse_map, "código de almacén"),
        ("producto", staged["producto"], "sku", product_map, "SKU"),
        ("cliente", staged["cliente"], "tax_id", customer_map, "RUC/ID fiscal"),
        ("vehiculo", staged["vehiculo"], "plate", vehicle_map, "placa"),
        ("conductor", staged["conductor"], "license_number", driver_map, "número de licencia"),
        ("pedido", staged["pedido"], "number", order_map, "número de pedido"),
        ("viaje", staged["viaje"], "code", trip_map, "código de viaje"),
    )
    for _, items, key_name, existing_map, label in entity_data:
        for item in items:
            if item[key_name] in existing_map:
                errors.append(f"Fila {item['line']}: ya existe {label} '{item[key_name]}' en el ERP.")

    warehouse_keys = set(warehouse_map) | {item["code"] for item in staged["almacen"]}
    product_keys = set(product_map) | {item["sku"] for item in staged["producto"]}
    customer_keys = set(customer_map) | {item["tax_id"] for item in staged["cliente"]}
    vehicle_keys = set(vehicle_map) | {item["plate"] for item in staged["vehiculo"]}
    driver_keys = set(driver_map) | {item["license_number"] for item in staged["conductor"]}
    order_keys = set(order_map) | {item["number"] for item in staged["pedido"]}

    for item in staged["inventario"]:
        if item["warehouse_code"] not in warehouse_keys:
            errors.append(f"Fila {item['line']}: no existe el almacén '{item['warehouse_code']}' en el CSV ni en el ERP.")
        if item["sku"] not in product_keys:
            errors.append(f"Fila {item['line']}: no existe el producto '{item['sku']}' en el CSV ni en el ERP.")
    for item in staged["pedido"]:
        if item["customer_tax_id"] not in customer_keys:
            errors.append(f"Fila {item['line']}: no existe el cliente '{item['customer_tax_id']}' en el CSV ni en el ERP.")
        if item["sku"] not in product_keys:
            errors.append(f"Fila {item['line']}: no existe el producto '{item['sku']}' en el CSV ni en el ERP.")
    for item in staged["viaje"]:
        if item["plate"] and item["plate"] not in vehicle_keys:
            errors.append(f"Fila {item['line']}: no existe el vehículo '{item['plate']}' en el CSV ni en el ERP.")
        if item["license_number"] and item["license_number"] not in driver_keys:
            errors.append(f"Fila {item['line']}: no existe el conductor '{item['license_number']}' en el CSV ni en el ERP.")
        if item["order_number"] and item["order_number"] not in order_keys:
            errors.append(f"Fila {item['line']}: no existe el pedido '{item['order_number']}' en el CSV ni en el ERP.")

    inventory_pairs = set()
    inventory_ids = []
    for item in staged["inventario"]:
        warehouse = warehouse_map.get(item["warehouse_code"])
        product = product_map.get(item["sku"])
        if warehouse and product:
            inventory_ids.append((warehouse.id, product.id))
            inventory_pairs.add((warehouse.id, product.id, item["line"]))
    if inventory_ids:
        existing_pairs = set(
            db.session.query(InventoryBalance.warehouse_id, InventoryBalance.product_id)
            .filter(tuple_(InventoryBalance.warehouse_id, InventoryBalance.product_id).in_(inventory_ids))
            .all()
        )
        for warehouse_id, product_id, line_number in inventory_pairs:
            if (warehouse_id, product_id) in existing_pairs:
                errors.append(f"Fila {line_number}: ya existe un saldo para ese almacén y producto.")

    if errors:
        raise CsvImportError(errors[:50])

    new_warehouses = {item["code"]: Warehouse(code=item["code"], name=item["name"], city=item["city"]) for item in staged["almacen"]}
    new_products = {item["sku"]: Product(sku=item["sku"], name=item["name"], unit=item["unit"]) for item in staged["producto"]}
    new_customers = {
        item["tax_id"]: Customer(tax_id=item["tax_id"], business_name=item["business_name"], email=item["email"], contact_name=item["contact_name"])
        for item in staged["cliente"]
    }
    new_vehicles = {
        item["plate"]: Vehicle(plate=item["plate"], vehicle_type=item["vehicle_type"], capacity_kg=item["capacity_kg"])
        for item in staged["vehiculo"]
    }
    new_drivers = {
        item["license_number"]: Driver(license_number=item["license_number"], name=item["name"], phone=item["phone"])
        for item in staged["conductor"]
    }
    db.session.add_all(
        list(new_warehouses.values()) + list(new_products.values()) + list(new_customers.values())
        + list(new_vehicles.values()) + list(new_drivers.values())
    )
    try:
        db.session.flush()
        warehouse_map.update(new_warehouses)
        product_map.update(new_products)
        customer_map.update(new_customers)
        vehicle_map.update(new_vehicles)
        driver_map.update(new_drivers)

        new_balances = []
        new_movements = []
        for item in staged["inventario"]:
            warehouse = warehouse_map[item["warehouse_code"]]
            product = product_map[item["sku"]]
            new_balances.append(InventoryBalance(warehouse=warehouse, product=product, quantity=item["quantity"]))
            new_movements.append(
                StockMovement(
                    warehouse=warehouse,
                    product=product,
                    user_id=user_id,
                    movement_type="entrada",
                    quantity=item["quantity"],
                    reason="Carga inicial CSV",
                )
            )

        new_orders = []
        for item in staged["pedido"]:
            order = SalesOrder(
                number=item["number"],
                customer=customer_map[item["customer_tax_id"]],
                status=item["status"],
                created_by_id=user_id,
            )
            order.lines.append(OrderLine(product=product_map[item["sku"]], quantity=item["quantity"]))
            new_orders.append(order)
        db.session.add_all(new_balances + new_movements + new_orders)
        db.session.flush()
        order_map.update({order.number: order for order in new_orders})

        new_trips = []
        for item in staged["viaje"]:
            order = order_map.get(item["order_number"]) if item["order_number"] else None
            if item["status"] == "completado" and order and order.status != "cancelado":
                order.status = "entregado"
            trip = Trip(
                code=item["code"],
                origin=item["origin"],
                destination=item["destination"],
                vehicle=vehicle_map.get(item["plate"]) if item["plate"] else None,
                driver=driver_map.get(item["license_number"]) if item["license_number"] else None,
                order=order,
                status=item["status"],
                departure_at=utcnow_naive() if item["status"] in {"en_ruta", "completado"} else None,
            )
            new_trips.append(trip)

        db.session.add_all(new_trips)
        counts = Counter({kind: len(items) for kind, items in staged.items()})
        enqueue_event("erp.csv.import_completed", "csv_import", uuid4(), {
            "counts": dict(counts),
            "row_count": row_count,
        })
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise CsvImportError(["La carga se canceló porque uno o más registros entraron en conflicto con datos existentes. No se guardó ninguna fila."]) from exc
    except Exception:
        db.session.rollback()
        raise

    return dict(counts)
