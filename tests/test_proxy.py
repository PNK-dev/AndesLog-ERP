import re

from flask import request, session

from app import create_app
from app.models import User, db


def make_app(tmp_path, *, trust_proxy):
    app = create_app({"TESTING": True, "SECRET_KEY": "local-proxy-test-key",
                      "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'proxy.db'}",
                      "TRUST_PROXY_PROTO": trust_proxy, "SESSION_COOKIE_SECURE": True,
                      "WTF_CSRF_ENABLED": True})
    @app.get("/test-proxy")
    def inspect_request():
        session["test"] = True
        return {"scheme": request.scheme, "host": request.host, "client": request.remote_addr}
    with app.app_context():
        db.create_all()
        user = User(username="proxy-test", role="admin")
        user.set_password("local-proxy-password")
        db.session.add(user)
        db.session.commit()
    return app


def test_proxy_headers_are_not_trusted_by_default(tmp_path):
    app = make_app(tmp_path, trust_proxy=False)
    response = app.test_client().get("/test-proxy", base_url="http://erp.example",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example", "X-Forwarded-For": "1.2.3.4"})
    assert response.json == {"scheme": "http", "host": "erp.example", "client": "127.0.0.1"}


def test_trusted_proxy_only_changes_protocol(tmp_path):
    app = make_app(tmp_path, trust_proxy=True)
    response = app.test_client().get("/test-proxy", base_url="http://erp.example",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example", "X-Forwarded-For": "1.2.3.4"})
    assert response.json == {"scheme": "https", "host": "erp.example", "client": "127.0.0.1"}
    assert "Secure" in response.headers["Set-Cookie"]


def test_csrf_login_with_tls_terminated_by_proxy(tmp_path):
    app = make_app(tmp_path, trust_proxy=True)
    client = app.test_client()
    headers = {"X-Forwarded-Proto": "https", "Referer": "https://erp.example/login"}
    page = client.get("/login", base_url="http://erp.example", headers=headers)
    csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
    bad = client.post("/login", base_url="http://erp.example", headers=dict(headers, Referer="https://evil.example/login"),
                      data={"username": "proxy-test", "password": "local-proxy-password", "csrf_token": csrf})
    assert bad.status_code == 400
    good = client.post("/login", base_url="http://erp.example", headers=headers,
                       data={"username": "proxy-test", "password": "local-proxy-password", "csrf_token": csrf})
    assert good.status_code == 302
    assert "Secure" in good.headers["Set-Cookie"]
