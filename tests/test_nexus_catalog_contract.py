from __future__ import annotations

from backend.nexus_api import NexusCatalog, NexusError


class FakeCatalog:
    def __init__(self):
        self.calls = []

    def popular(self, sort="downloads", force=False, count=24):
        self.calls.append(("popular", sort, force, count))
        return {"items": [], "source": "live", "stale": False, "fetched_at": "now", "warning": ""}

    def search(self, keyword, force=False, count=24):
        self.calls.append(("search", keyword, force, count))
        return {"items": [], "source": "live", "stale": False, "fetched_at": "now", "warning": ""}

    def get(self, mod_id, force=False):
        self.calls.append(("get", mod_id, force))
        return {"items": [], "source": "live", "stale": False, "fetched_at": "now", "warning": ""}


def test_nexus_routes_keep_authentication(app):
    assert app.test_client().get("/api/nexus/popular").status_code == 403


def test_nexus_routes_return_catalog_envelope_and_forward_parameters(app, auth_client):
    fake = FakeCatalog()
    app.extensions["nexus_catalog"] = fake

    response = auth_client.get("/api/nexus/popular?sort=endorsements&count=50&force=1")
    assert response.status_code == 200
    assert response.json["data"]["source"] == "live"
    assert fake.calls == [("popular", "endorsements", True, 50)]

    assert auth_client.get("/api/nexus/latest?count=2").status_code == 200
    assert fake.calls[-1] == ("popular", "latest", False, 2)
    assert auth_client.get("/api/nexus/search?q=abc&count=3&force=true").status_code == 200
    assert fake.calls[-1] == ("search", "abc", True, 3)
    assert auth_client.get("/api/nexus/mod/123?force=1").status_code == 200
    assert fake.calls[-1] == ("get", 123, True)


def test_nexus_api_response_excludes_adult_content_and_metadata(app, auth_client, tmp_path):
    def transport(_query, _variables):
        return {"data": {"mods": {"nodes": [{
            "modId": 42,
            "name": "SECRET ADULT NAME",
            "summary": "SECRET ADULT SUMMARY",
            "pictureUrl": "https://secret.example/adult.webp",
            "author": "SECRET ADULT AUTHOR",
            "adultContent": True,
        }]}}}

    app.extensions["nexus_catalog"] = NexusCatalog(tmp_path / "nexus-cache", transport=transport)
    response = auth_client.get("/api/nexus/popular")

    assert response.status_code == 200
    assert response.json["data"]["items"] == []
    assert "SECRET ADULT" not in response.get_data(as_text=True)


def test_nexus_route_parameter_errors_are_400(app, auth_client):
    fake = FakeCatalog()
    app.extensions["nexus_catalog"] = fake
    for path in (
        "/api/nexus/popular?count=0", "/api/nexus/popular?count=51",
        "/api/nexus/popular?count=nope", "/api/nexus/popular?sort=unsafe",
        "/api/nexus/popular?force=maybe",
    ):
        response = auth_client.get(path)
        assert response.status_code == 400, path
        assert response.json["error_code"] == "invalid_input"
    assert fake.calls == []


def test_nexus_disk_error_is_safe_502_without_path(app, auth_client):
    class BrokenDisk(FakeCatalog):
        def popular(self, **kwargs):
            raise OSError(r"permission denied: C:\\Users\\secret\\cache.json")

    app.extensions["nexus_catalog"] = BrokenDisk()
    response = auth_client.get("/api/nexus/popular")
    assert response.status_code == 502
    assert response.json["error"] == "Nexus 请求失败"
    assert "secret" not in response.get_data(as_text=True)


def test_non_nexus_error_is_safe_502(app, auth_client):
    class BrokenUnexpected(FakeCatalog):
        def popular(self, **kwargs):
            raise ValueError(r"bad internal path C:\\private\\cache")

    app.extensions["nexus_catalog"] = BrokenUnexpected()
    response = auth_client.get("/api/nexus/popular")
    assert response.status_code == 502
    assert response.json["error"] == "Nexus 请求失败"
    assert "private" not in response.get_data(as_text=True)


def test_nexus_upstream_error_is_readable_502(app, auth_client):
    class Broken(FakeCatalog):
        def popular(self, **kwargs):
            raise NexusError("Nexus timed out")

    app.extensions["nexus_catalog"] = Broken()
    response = auth_client.get("/api/nexus/popular")
    assert response.status_code == 502
    assert response.json["error"] == "Nexus timed out"
