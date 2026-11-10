import socket
import threading
import time

from app import create_app


def test_readiness_returns_503_when_postgresql_handshake_stalls():
    # A listening socket accepts TCP but never completes the PostgreSQL
    # handshake, representing a dependency that is reachable yet unresponsive.
    stop = threading.Event()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(10)
    port = server.getsockname()[1]

    def stalled_server():
        try:
            conn, _ = server.accept()
            with conn:
                stop.wait(12)
        except OSError:
            pass

    worker = threading.Thread(target=stalled_server, daemon=True)
    worker.start()
    try:
        app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-only",
            "SQLALCHEMY_DATABASE_URI": f"postgresql+psycopg://test@127.0.0.1:{port}/test?sslmode=disable",
        })
        started = time.monotonic()
        response = app.test_client().get("/api/health/ready")
        elapsed = time.monotonic() - started
        assert response.status_code == 503
        assert elapsed < 10
    finally:
        stop.set()
        server.close()
        worker.join(timeout=2)
