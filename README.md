# AndesLog ERP

Monolito modular local para una empresa logística B2B. El primer MVP cubre acceso de usuarios, almacenes, catálogo de productos, inventario, clientes, pedidos y planificación de viajes. Está preparado para ejecutarse con Docker Compose y PostgreSQL.

## Estado del MVP

- Interfaz web en español adaptable a escritorio y móvil.
- Login con sesión segura, hash de contraseñas, protección CSRF y roles `admin`/`operator`.
- Gestión inicial de almacenes, productos y clientes B2B.
- Movimientos de entrada/salida con trazabilidad y bloqueo de salidas que dejarían stock negativo.
- Pedidos con estado y una línea de producto en este MVP.
- Tráfico básico con conductores, vehículos, rutas, pedidos asociados y estados de viaje. Al completar un viaje se marca el pedido asociado como entregado.
- Resumen de operación y API JSON mínima (`/api/health`, `/api/v1/summary`).
- PostgreSQL para el entorno local mediante Docker Compose. No se necesita instalar PostgreSQL en Windows.

Es un prototipo académico/MVP, todavía no es un ERP listo para producción. No incluye facturación, costos, guías de remisión, liquidación de transportistas, permisos granulares, auditoría completa ni conciliación automática de pedidos contra stock.

## Requisitos locales

- Docker Desktop con Docker Compose v2 y el motor iniciado.
- Navegador moderno.

## Clonar y ejecutar desde otro equipo

El repositorio no depende de una ruta fija. Clónalo en cualquier carpeta y crea tu configuración local a partir del ejemplo:

```powershell
git clone <URL_REPOSITORIO_ERP> AndesLog-ERP
Set-Location .\AndesLog-ERP
Copy-Item .env.example .env
```

En macOS o Linux, reemplaza el último comando por `cp .env.example .env`. Edita `.env` con secretos distintos y aleatorios para ese equipo; nunca lo agregues a Git. Requiere Git, Docker Engine y el plugin Docker Compose v2. Ejecuta `docker compose up --build -d` y abre `http://localhost:8000`.
## Arranque rápido en Windows PowerShell

Después de crear `.env` desde `.env.example`, edítalo y reemplaza `SECRET_KEY`, `POSTGRES_PASSWORD` y `BOOTSTRAP_ADMIN_PASSWORD` por valores aleatorios. La contraseña del administrador inicial debe tener al menos 12 caracteres. No subas `.env` a Git ni reutilices estas credenciales en la nube.

Desde `ERP-APP`:

```powershell
docker compose up --build -d
docker compose ps
```

Abre <http://localhost:8000>. La API responde en `http://localhost:8000/api/health`.

Para ver registros:

```powershell
docker compose logs -f web
```

Para detener los contenedores sin borrar la base local:

```powershell
docker compose down
```

Para borrar también el volumen PostgreSQL y todos los datos locales (acción destructiva):

```powershell
docker compose down -v
```

El puerto PostgreSQL se publica solo en `127.0.0.1:5433`; la web solo en `127.0.0.1:8000`.

## Cuenta de inicio local

El contenedor crea el administrador si aún no existe, usando `BOOTSTRAP_ADMIN_USERNAME` y `BOOTSTRAP_ADMIN_PASSWORD` de `.env`. El valor inicial generado para este entorno está en ese archivo local; no lo compartas ni lo uses fuera del laboratorio.

La aplicación genera el hash de la contraseña con Werkzeug y nunca guarda la contraseña en claro en PostgreSQL. Desde el menú **Usuarios**, el administrador puede crear cuentas de operadores o administradores. Se recomienda cambiar la clave inicial luego del primer acceso.

## Pruebas

Las pruebas usan SQLite temporal; no necesitan iniciar ni borrar PostgreSQL. Con Docker iniciado:

```powershell
docker compose build web
docker compose run --rm --no-deps web python -m pytest -q
```

Las pruebas cubren acceso autenticado, autorización por rol, recepción y salida de stock, rechazo de inventario negativo, creación de pedidos y cierre de entrega desde tráfico.

## Estructura

```text
app/                 aplicación Flask, modelos, rutas, plantillas y CSS
migrations/          migraciones de esquema Alembic/Flask-Migrate
 tests/               pruebas locales con pytest
Dockerfile            imagen reproducible de la aplicación
compose.yaml          web + PostgreSQL local
.env                  configuración local ignorada por Git
```

## Datos y persistencia

Compose guarda PostgreSQL en el volumen nombrado `postgres_data`. Reconstruir la imagen o recrear el contenedor web no borra ese volumen. Las migraciones versionadas actualizan el esquema de la base. La base local sirve para desarrollo; al desplegar en AWS se configurará el `DATABASE_URL` privado de PostgreSQL en la EC2 existente y se ejecutará el mismo comando de migración durante una etapa controlada.

## Migración a la nube con Docker

1. Crear y probar una imagen inmutable del ERP; publicarla en un registro permitido por la cuenta académica.
2. Terraform mantiene redes, EC2, balanceador y PostgreSQL; no contiene código de negocio ni contraseñas de usuario.
3. Instalar Docker Engine/Compose en las EC2 web mediante el bootstrap de la instancia y desplegar la misma imagen/configuración que se probó localmente.
4. Configurar el ERP con variables de entorno: `DATABASE_URL`, `SECRET_KEY`, `BOOTSTRAP_ADMIN_USERNAME` y `BOOTSTRAP_ADMIN_PASSWORD`. No almacenar contraseñas en Git ni en archivos `.tfvars`; el manejo final de secretos debe elegirse según permisos de AWS Academy.
5. Ejecutar migraciones una vez contra PostgreSQL, validar `/api/health`, login y flujos básicos detrás del ALB.
6. Mantener PostgreSQL en la instancia y volumen EBS ya existentes; no publicar el puerto 5432 a Internet. Hacer backup antes de cambios del esquema.

El compose de este directorio es para desarrollo local. El despliegue remoto se añadirá en la fase de migración cuando confirmemos qué registro de imágenes y mecanismo de secretos permite la cuenta académica. La API actual de infraestructura que ya vive en `Terraform/app` no debe sincronizarse automáticamente con este ERP; esta carpeta será el nuevo origen de la aplicación.

## Publicar una imagen para Terraform

La infraestructura consume una imagen del ERP publicada en un registro de contenedores; no necesita la carpeta del código ni una ruta absoluta de Windows. Construye y publica una versión identificada, no `latest`:

```powershell
$image = "ghcr.io/<usuario-o-equipo>/andeslog-erp:1.0.0"
docker build -t $image .
docker push $image
```

En GitHub Container Registry, cambia la visibilidad del paquete a **Public** si se usará con el Terraform actual: las instancias AWS hacen `docker pull` anónimo. GHCR crea los paquetes nuevos como privados inicialmente. Si eliges un registro privado, habrá que modificar el bootstrap de AWS para autenticarse sin incluir credenciales en Terraform. Configura la misma referencia versionada en `erp_image` dentro del `terraform.tfvars` local de AWS. Para despliegues reproducibles, prefiere una referencia por digest cuando ya esté publicada.

Para versionar el repositorio, inicialízalo y enlázalo al remoto que elijas:

```powershell
git init -b main
git add .
git commit -m "Initial AndesLog ERP"
git remote add origin <URL_REPOSITORIO_ERP>
git push -u origin main
```

`git add .` excluye `.env`, bases locales, cachés y archivos compilados según `.gitignore`. Revisa `git status --short` antes del primer `commit`.

## Configuración del entorno

| Variable | Propósito |
|---|---|
| `APP_ENV` | `development` local; usar `production` al desplegar para activar cookies Secure |
| `SECRET_KEY` | Firma de sesiones Flask; secreta y única por ambiente |
| `DATABASE_URL` | Conexión SQLAlchemy. Compose la arma con el servicio PostgreSQL local |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Base local del servicio `db` |
| `BOOTSTRAP_ADMIN_USERNAME`, `BOOTSTRAP_ADMIN_PASSWORD` | Cuenta inicial local, solo se crea si no existe |

## Referencias oficiales

- [Docker Compose](https://docs.docker.com/compose/): ciclo de vida multi-contenedor.
- [GitHub: publicar e instalar imágenes de contenedor en GHCR](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).
- [Flask deployment](https://flask.palletsprojects.com/en/stable/deploying/): servidor WSGI; esta imagen usa Gunicorn.
- [Flask-Migrate](https://flask-migrate.readthedocs.io/en/latest/): migraciones SQLAlchemy mediante Alembic.
- [PostgreSQL: roles y usuarios](https://www.postgresql.org/docs/current/user-manag.html): roles y privilegios de base de datos.

