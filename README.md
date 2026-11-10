# AndesLog ERP

Monolito modular local para una empresa logística B2B. El primer MVP cubre acceso de usuarios, almacenes, catálogo de productos, inventario, clientes, pedidos y planificación de viajes. Está preparado para ejecutarse con Docker Compose y PostgreSQL.

## Estado del MVP

- Interfaz web en español adaptable a escritorio y móvil.
- Login con sesión segura, hash de contraseñas, protección CSRF y roles `admin`/`operator`.
- Gestión inicial de almacenes, productos y clientes B2B.
- Movimientos de entrada/salida con trazabilidad y bloqueo de salidas que dejarían stock negativo.
- Pedidos con estado y una línea de producto en este MVP.
- Tráfico básico con conductores, vehículos, rutas, pedidos asociados y estados de viaje. Al completar un viaje se marca el pedido asociado como entregado.
- Resumen de operación y API JSON mínima (`/api/health/live`, `/api/health/ready`, `/api/v1/summary`).
- Importación masiva de datos simulados desde CSV, con validación y transacción completa.
- PostgreSQL para el entorno local mediante Docker Compose. No se necesita instalar PostgreSQL en Windows.

Es un prototipo académico/MVP, todavía no es un ERP listo para producción. No incluye facturación, costos, guías de remisión, liquidación de transportistas, permisos granulares, auditoría completa ni conciliación automática de pedidos contra stock.

## Importar datos desde CSV

Inicia sesión con una cuenta administradora y abre **Importar CSV** en el menú. Puedes descargar desde esa pantalla `andeslog_demo.csv`, que contiene datos completamente ficticios para poblar los ocho módulos del MVP, o preparar un archivo propio con la misma estructura.

El archivo debe guardarse como CSV UTF-8 y usar estos encabezados exactos:

```text
tipo,codigo,nombre,ciudad,email,contacto,unidad,almacen,producto,cantidad,capacidad_kg,telefono,cliente,estado,origen,destino,vehiculo,conductor,pedido
```

Cada fila lleva un valor `tipo`: `almacen`, `producto`, `cliente`, `inventario`, `vehiculo`, `conductor`, `pedido` o `viaje`. Los demás campos se completan según el tipo; la pantalla del ERP muestra qué columnas usa cada uno. Las filas pueden aparecer en cualquier orden. Los códigos relacionan los datos, por ejemplo, `almacen` y `producto` en una fila de inventario deben coincidir con un almacén y un SKU del mismo CSV o ya existentes en el ERP.

La carga está limitada a 2 MB y 500 filas. Se validan encabezados, datos obligatorios, cantidades, estados, referencias y códigos repetidos. Si hay errores o algún identificador ya existe, se rechaza el archivo completo y no se guarda ninguna fila. Los saldos iniciales también generan movimientos de entrada asociados al administrador que realizó la importación. El CSV de ejemplo está pensado para una base vacía; no lo importes dos veces.

En local, reconstruye y reinicia la aplicación para incluir el importador:

```powershell
docker compose up --build -d
```

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

La aplicación incluye protección CSRF, hash de contraseñas, cookies de sesión `HttpOnly` y `SameSite=Lax`, y cabeceras CSP, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` y `Permissions-Policy`. En producción, usa `APP_ENV=production` detrás de HTTPS para activar la marca `Secure` en la cookie de sesión. Las cabeceras no sustituyen TLS: el listener del ALB requiere un dominio propio y un certificado ACM para habilitar HTTPS.

Existe una opción experimental `TRUST_PROXY_PROTO=true` para reconocer `X-Forwarded-Proto` de **un único proxy confiable** que termina TLS. Solo se debe activar si la aplicación no es accesible directamente desde clientes y las reglas de red garantizan que las solicitudes lleguen exclusivamente por ese proxy; de lo contrario un cliente puede falsificar la cabecera. No confía en las cabeceras de IP, host, puerto ni prefijo. Se probó con sesiones y CSRF en local, no en AWS; el script del laboratorio sigue usando HTTP y no configura esta opción de producción. [Guía de Flask para proxies](https://flask.palletsprojects.com/en/stable/deploying/proxy_fix/).

Desde `ERP-APP`:

```powershell
docker compose up --build -d
docker compose ps
```

Abre <http://localhost:8000>. La comprobación de vida del proceso responde en `http://localhost:8000/api/health/live`. La disponibilidad se consulta en `http://localhost:8000/api/health/ready` (también `/api/health`): con PostgreSQL devuelve 503 si el servidor está en recuperación o si la transacción es de solo lectura. La comprobación no escribe ni garantiza permisos suficientes, integridad de datos o ausencia de dos primarios. SQLite conserva la comprobación básica de conexión.

Las conexiones nuevas a PostgreSQL usan un tiempo máximo de conexión de cinco segundos por host, salvo que `DATABASE_URL` especifique `connect_timeout`. Esto limita la espera al establecer conexiones; no limita consultas en curso ni garantiza por sí solo el tiempo total de recuperación. Consulta la [documentación de libpq](https://www.postgresql.org/docs/current/libpq-connect.html).

El formulario de inventario asigna un identificador a cada operación. Si se reenvía el mismo formulario con los mismos datos, el ERP reconoce el movimiento y no lo registra dos veces; si se reutiliza el identificador con datos distintos devuelve conflicto. La protección depende de que el registro exista en la base recuperada y de reenviar el mismo formulario: abrir uno nuevo genera otro identificador. Si una réplica perdió la transacción, un reintento puede volver a registrarla; su evento intercloud conserva el identificador derivado, pero si Azure ya recibió otro contenido con ese ID se necesita reconciliación manual. Otras operaciones todavía no tienen esta protección. La migración agrega un campo opcional para respetar movimientos históricos y no permite revertirla si ya existen identificadores que se perderían.

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

Las pruebas cubren acceso autenticado, autorización por rol, recepción y salida de stock, rechazo de inventario negativo, creación de pedidos, tráfico y entrega de eventos. Las pruebas opcionales de concurrencia y restauración usan PostgreSQL temporal en `127.0.0.1` y se omiten si faltan los binarios correspondientes; no necesitan recursos cloud. La restauración local comprueba datos sintéticos, migraciones y login, pero no acredita la recuperación de un backup RDS real.

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

Compose guarda PostgreSQL en el volumen nombrado `postgres_data`. Reconstruir la imagen o recrear el contenedor web no borra ese volumen. Las migraciones versionadas actualizan el esquema. La base local sirve para desarrollo; el Terraform actual define RDS PostgreSQL privado y ejecuta la migración en una fase controlada. Consulta el README del repositorio Terraform para sus requisitos y limitaciones vigentes.

## Migración a la nube con Docker

1. Crear y probar una imagen inmutable del ERP; publicarla en un registro permitido por la cuenta académica.
2. Terraform mantiene redes, EC2, balanceador y RDS; el estado puede contener valores sensibles y debe protegerse.
3. Instalar Docker Engine/Compose en las EC2 web mediante el bootstrap de la instancia y desplegar la misma imagen/configuración que se probó localmente.
4. Configurar el ERP con `DATABASE_URL`, `SECRET_KEY`, `BOOTSTRAP_ADMIN_USERNAME` y `BOOTSTRAP_ADMIN_PASSWORD`. El flujo Terraform prepara secretos de ejecución para AWS Secrets Manager y un archivo local ignorado por Git; protege ambos y revisa los permisos de AWS Academy.
5. Ejecutar migraciones una vez contra PostgreSQL, validar `/api/health`, login y flujos básicos detrás del ALB.
6. Mantener PostgreSQL en RDS privado sin publicar el puerto 5432 a Internet. Hacer backup y probar restauración antes de cambios del esquema.

El compose de este directorio es para desarrollo local. El despliegue y la publicación de la imagen se gestionan desde el repositorio Terraform hermano; revisa allí el registro, las credenciales temporales y las opciones actuales antes de desplegar.

## Publicar una imagen para Terraform

El flujo Terraform actual construye este proyecto hermano y publica una imagen con etiqueta inmutable `img-...` en ECR privado. Las instancias necesitan permisos para descargarla. Ejecuta la publicación mediante el flujo documentado en el README del repositorio Terraform; publicar una imagen GHCR por sí sola no actualiza el despliegue configurado actualmente. En un clon nuevo, verifica la ubicación de ambos repositorios y las credenciales de AWS antes de desplegar.

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
| `TRUST_PROXY_PROTO` | Experimental: `true` solo detrás de un proxy confiable único con acceso al backend restringido; por defecto `false` |
| `SECRET_KEY` | Firma de sesiones Flask; secreta y única por ambiente |
| `DATABASE_URL` | Conexión SQLAlchemy. Compose la arma con el servicio PostgreSQL local |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Base local del servicio `db` |
| `BOOTSTRAP_ADMIN_USERNAME`, `BOOTSTRAP_ADMIN_PASSWORD` | Cuenta inicial local, solo se crea si no existe |
| `ERP_AZURE_INGEST_URL` | Endpoint HTTPS de integración de eventos con Azure |
| `ERP_AZURE_INGEST_HMAC_SECRET` | Secreto de firma para la integración; no incluir en Git ni registros |
| `ERP_AZURE_INGEST_FALLBACK_URL` | Endpoint regional alternativo experimental; vacío lo desactiva |
| `ERP_AZURE_INGEST_FALLBACK_HMAC_SECRET` | Secreto HMAC independiente para el endpoint alternativo |
| `OUTBOX_POLL_SECONDS` | Intervalo del worker cuando no hay eventos disponibles |

## Integración por eventos

El ERP registra eventos en PostgreSQL junto con los cambios de negocio. El worker los envía con HMAC y los reintenta ante fallas temporales; Azure acepta duplicados por UUID. El worker solo marca `delivered` si recibe un JSON con el mismo ID y estado `stored` o `duplicate`, y no sigue redirecciones HTTP. Si vence el lease y otro worker reclama el evento, el intento anterior ya no puede cambiar su estado usando esa reclamación.

La entrega puede repetirse, no garantiza orden global y no demuestra recuperación ante pérdida regional. Los errores permanentes pasan a `dead` y requieren revisión. Para nuevos fallos temporales, el worker usa espera exponencial con variación aleatoria y un máximo habitual de 60 segundos; si el servidor envía un `Retry-After` válido más largo, lo respeta. Esto no garantiza que un evento se entregue en 60 segundos: influyen el timeout, la cola, las caídas y la capacidad del servicio. Los eventos que ya tenían una espera larga con el código anterior conservan su fecha; deben revisarse durante la actualización. Los reintentos más frecuentes pueden aumentar solicitudes y carga. El bloqueo de intentos obsoletos depende de la consistencia de una misma base y no sustituye fencing entre primarios separados. Estos cambios se probaron localmente y requieren una nueva imagen y una prueba de integración en nube antes de considerarlos desplegados. Referencias: [Transactional outbox de AWS](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) y [manejo de fallos transitorios de Azure](https://learn.microsoft.com/en-us/azure/architecture/best-practices/transient-faults).

La ruta regional alternativa es experimental: ante errores de red, respuesta inválida o HTTP 408/425/429/5xx del destino primario, intenta el secundario con el mismo evento y una clave independiente. La migración `2f5b9c6d7a10` registra si el comprobante llegó de `primary` o `secondary`; los registros antiguos `NULL` corresponden al flujo previo sin destino secundario. No se siguen redirecciones y los endpoints externos configurados deben usar HTTPS. Si ambos destinos fallan, el evento permanece pendiente; un error permanente del primario no cambia automáticamente de región. El archivo secundario de ingesta y la réplica del archivo primario son contenedores distintos: una respuesta perdida puede dejar el mismo evento en ambos y todavía no existe reconciliación ni retorno automático al primario. La opción no debe activarse en producción hasta publicar y validar la Function secundaria y su recuperación. La reversión de la migración se bloquea si hay eventos registrados en el archivo secundario.

## Referencias oficiales

- [Docker Compose](https://docs.docker.com/compose/): ciclo de vida multi-contenedor.
- [GitHub: publicar e instalar imágenes de contenedor en GHCR](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).
- [Flask deployment](https://flask.palletsprojects.com/en/stable/deploying/): servidor WSGI; esta imagen usa Gunicorn.
- [Flask-Migrate](https://flask-migrate.readthedocs.io/en/latest/): migraciones SQLAlchemy mediante Alembic.
- [PostgreSQL: roles y usuarios](https://www.postgresql.org/docs/current/user-manag.html): roles y privilegios de base de datos.

