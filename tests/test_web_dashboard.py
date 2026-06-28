"""Тест статического дашборда: /dashboard раздаётся тем же FastAPI-приложением.

create_app строим с None-зависимостями — маршрут /dashboard их не трогает (это
StaticFiles-mount), а другие эндпоинты в этом тесте не вызываем.
"""
from fastapi.testclient import TestClient

from web.server import create_app


def _client() -> TestClient:
    app = create_app(None, None, None, None, None)  # type: ignore[arg-type]
    return TestClient(app)


def test_dashboard_serves_index_html():
    r = _client().get("/dashboard/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "J.A.R.V.I.S. Dashboard" in r.text


def test_dashboard_bare_path_reaches_index():
    # /dashboard без слэша → редирект на /dashboard/ → та же страница
    r = _client().get("/dashboard", follow_redirects=True)
    assert r.status_code == 200
    assert "Dashboard" in r.text


def test_api_route_still_works_alongside_mount():
    # mount /dashboard не должен перехватывать /api/* (read-only эндпоинт без сети)
    r = _client().get("/api/calendar/today")
    assert r.status_code == 200
    assert r.json() == []
