from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import nexus_api
from backend.nexus_api import NexusCatalog, NexusError


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, query, variables):
        self.calls.append((query, variables))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def payload(nodes):
    return {"data": {"mods": {"nodes": nodes}}}


def node(**overrides):
    value = {
        "modId": 42, "name": "Example", "summary": "Summary",
        "pictureUrl": "https://img.example/mod.webp", "author": "Alice",
        "version": "1.2", "downloads": 1200, "endorsements": 34,
        "createdAt": "2025-01-01", "updatedAt": "2025-02-01",
        "adultContent": False, "url": "https://evil.example/phishing",
    }
    value.update(overrides)
    return value


def test_popular_normalizes_fields_and_never_trusts_response_url(tmp_path):
    transport = Transport([payload([node()])])
    result = NexusCatalog(tmp_path, transport=transport).popular("downloads")

    assert result["source"] == "live" and result["stale"] is False
    assert result["warning"] == ""
    assert result["items"] == [{
        "nexus_id": 42, "name": "Example", "summary": "Summary",
        "picture_url": "https://img.example/mod.webp", "author": "Alice",
        "version": "1.2", "downloads": 1200, "endorsements": 34,
        "created": "2025-01-01", "updated": "2025-02-01",
        "url": "https://www.nexusmods.com/palworld/mods/42", "adultContent": False,
    }]


def test_missing_fields_are_safe_and_non_https_images_are_removed(tmp_path):
    result = NexusCatalog(tmp_path, transport=Transport([payload([
        {"modId": 7, "pictureUrl": "http://unsafe.example/x"},
    ])])).popular()
    item = result["items"][0]
    assert item == {
        "nexus_id": 7, "name": "", "summary": "", "picture_url": "",
        "author": "", "version": "", "downloads": 0, "endorsements": 0,
        "created": "", "updated": "", "url": "https://www.nexusmods.com/palworld/mods/7",
        "adultContent": False,
    }


def test_search_uses_variables_so_injection_never_enters_query(tmp_path):
    attack = 'x\") { users { token } } #'
    transport = Transport([payload([])])
    NexusCatalog(tmp_path, transport=transport).search(attack)
    query, variables = transport.calls[0]
    assert attack not in query
    assert variables["keyword"] == attack
    assert variables["game"] == "palworld"


def test_numeric_search_uses_single_mod_query_and_variables(tmp_path):
    transport = Transport([{"data": {"mod": node(modId=123)}}])
    result = NexusCatalog(tmp_path, transport=transport).search("123")
    query, variables = transport.calls[0]
    assert "mods(" not in query and "mod(" in query
    assert variables["modId"] == 123 and variables["gameId"] == 6063
    assert result["items"][0]["nexus_id"] == 123


def test_graphql_errors_are_reported_not_converted_to_not_found(tmp_path):
    catalog = NexusCatalog(tmp_path, transport=Transport([{"errors": [{"message": "denied"}]}]))
    with pytest.raises(NexusError, match="GraphQL.*denied"):
        catalog.get(9)


def test_timeout_falls_back_to_last_success_as_stale_cache(tmp_path):
    transport = Transport([payload([node()]), TimeoutError("timed out")])
    catalog = NexusCatalog(tmp_path, transport=transport)
    live = catalog.popular(force=True)
    fallback = catalog.popular(force=True)
    assert fallback["source"] == "cache" and fallback["stale"] is True
    assert fallback["items"] == live["items"]
    assert "timed out" in fallback["warning"] and fallback["fetched_at"] == live["fetched_at"]


def test_fresh_cache_obeys_ttl_without_calling_transport(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(nexus_api.time, "time", lambda: now[0])
    transport = Transport([payload([node()]), payload([node(name="New")])])
    catalog = NexusCatalog(tmp_path, transport=transport, ttl=600)
    catalog.popular()
    now[0] += 599
    cached = catalog.popular()
    assert cached["source"] == "cache" and cached["stale"] is False
    assert len(transport.calls) == 1
    now[0] += 2
    refreshed = catalog.popular()
    assert refreshed["source"] == "live" and refreshed["items"][0]["name"] == "New"


def test_failure_without_cache_is_readable(tmp_path):
    catalog = NexusCatalog(tmp_path, transport=Transport([TimeoutError("timed out")]))
    with pytest.raises(NexusError, match="timed out"):
        catalog.popular(force=True)


def test_cache_keys_are_hashed_and_distinguish_requests(tmp_path):
    transport = Transport([payload([]), payload([]), {"data": {"mod": None}}])
    catalog = NexusCatalog(tmp_path, transport=transport)
    catalog.popular("latest")
    catalog.search("hello")
    catalog.get(5)
    names = [path.name for path in Path(tmp_path).glob("*.json")]
    assert len(names) == 3
    assert all(len(name) == 69 and name.endswith(".json") for name in names)
    assert not any("hello" in name or "latest" in name for name in names)


def test_invalid_sort_count_and_id_are_rejected_before_transport(tmp_path):
    transport = Transport([])
    catalog = NexusCatalog(tmp_path, transport=transport)
    with pytest.raises(ValueError): catalog.popular("unsafe")
    with pytest.raises(ValueError): catalog.popular(count=0)
    with pytest.raises(ValueError): catalog.search("x", count=51)
    with pytest.raises(ValueError): catalog.get("1 or 1=1")
    assert transport.calls == []


def test_adult_content_is_preserved(tmp_path):
    result = NexusCatalog(tmp_path, transport=Transport([payload([node(adultContent=True)])])).popular()
    assert result["items"][0]["adultContent"] is True


def test_invalid_json_and_schema_errors_are_readable(tmp_path):
    with pytest.raises(NexusError, match="JSON"):
        NexusCatalog(tmp_path / "a", transport=Transport(["not-json"])).popular()
    with pytest.raises(NexusError, match="结构"):
        NexusCatalog(tmp_path / "b", transport=Transport([{"data": {"mods": []}}])).popular()
