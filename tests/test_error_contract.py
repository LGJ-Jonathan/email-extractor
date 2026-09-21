"""Every error the API can return has the one envelope (app/errors.py).

The portal maps error.code to what the user sees, so codes are a contract: this file
pins each one that can be produced without a database.
"""

import pytest
from fastapi.testclient import TestClient

from app.errors import ERROR_CODES
from app.main import app
from app.settings import settings

client = TestClient(app, raise_server_exceptions=False)


def check(r, status: int, code: str):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"error", "detail", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["message"] and body["detail"] == body["error"]["message"]
    assert r.headers["x-request-id"] == body["request_id"]
    assert status == ERROR_CODES.get(code, (status,))[0]
    return body


def h(key):
    return {"X-API-Key": key}


def test_missing_key():
    check(client.get("/jobs"), 401, "missing_api_key")


def test_invalid_key():
    # (an ee_ key needs the database; that case is in test_queue_pg.py)
    check(client.get("/jobs", headers=h("not-our-format")), 401, "invalid_api_key")


def test_a_key_lookup_with_the_database_down_is_503_not_401():
    check(client.get("/jobs", headers=h("ee_looks-real")), 503, "service_unavailable")


def test_acting_user_is_refused_from_a_non_service_key(api_key):
    r = client.get("/jobs/abc", headers={**h(api_key), "X-Acting-User": "a@b.com"})
    check(r, 403, "acting_user_not_allowed")


def test_invalid_json(api_key):
    r = client.post("/jobs", content=b"{", headers={**h(api_key),
                                                    "content-type": "application/json"})
    check(r, 400, "invalid_json")


def test_validation_error_names_the_field(api_key):
    body = check(client.post("/jobs", json={"items": 5}, headers=h(api_key)),
                 422, "validation_error")
    assert body["error"]["details"][0]["field"] == "items"
    body = check(client.post("/extract", json={}, headers=h(api_key)), 422, "validation_error")
    assert body["error"]["details"][0]["field"] == "url"


def test_too_many_items(api_key, monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "MAX_ITEMS_PER_JOB", 3)
    body = check(client.post("/jobs", json={"items": ["a.com"] * 4}, headers=h(api_key)),
                 413, "too_many_rows")
    assert body["error"]["details"]["limit"] == 3


def test_file_too_large(api_key, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 100)
    files = {"file": ("x.csv", b"website\n" + b"a.com\n" * 50, "text/csv")}
    check(client.post("/jobs/preview", files=files, headers=h(api_key)), 413, "file_too_large")


def test_body_too_large_from_the_middleware(api_key):
    r = client.post("/jobs", content=b"x", headers={
        **h(api_key), "content-type": "application/json",
        "content-length": str(settings.max_upload_bytes * 2)})
    check(r, 413, "file_too_large")


@pytest.mark.parametrize("name", ["leads.pdf", "leads.xls", "leads.docx"])
def test_unsupported_file_type(api_key, name):
    files = {"file": (name, b"website\nacme.com\n", "application/octet-stream")}
    check(client.post("/jobs/preview", files=files, headers=h(api_key)),
          415, "unsupported_file_type")


def test_empty_file(api_key):
    files = {"file": ("x.csv", b"", "text/csv")}
    check(client.post("/jobs/preview", files=files, headers=h(api_key)), 422, "empty_file")


def test_corrupt_xlsx_is_invalid_file(api_key):
    files = {"file": ("x.xlsx", b"not a zip", "application/vnd.ms-excel")}
    check(client.post("/jobs/preview", files=files, headers=h(api_key)), 422, "invalid_file")


def test_extract_invalid_input_is_a_200_result_not_an_error(api_key):
    r = client.post("/extract", json={"url": "Acme Roofing LLC"}, headers=h(api_key))
    assert r.status_code == 200
    assert r.json()["status"] == "invalid_input" and r.json()["error_reason"] == "not_a_url"


def test_provider_quota_exhausted(api_key):
    from unittest.mock import patch

    from app.pipeline.fetch import ErrorClass, FetchError

    async def dead(*a, **k):
        raise FetchError(ErrorClass.PROVIDER_ACCOUNT, "provider_account", detail="402")

    with patch("app.main.process_input", dead):
        check(client.post("/extract", json={"url": "acme.com"}, headers=h(api_key)),
              503, "provider_quota_exhausted")


def test_unknown_route_and_wrong_method(api_key):
    check(client.get("/nope", headers=h(api_key)), 404, "not_found")
    check(client.get("/extract", headers=h(api_key)), 405, "method_not_allowed")


def test_unexpected_exception_is_500_without_internals(api_key):
    from unittest.mock import patch

    async def boom(*a, **k):
        raise RuntimeError("secret internals /etc/passwd")

    with patch("app.main.process_input", boom):
        body = check(client.post("/extract", json={"url": "acme.com"}, headers=h(api_key)),
                     500, "internal_error")
    assert "secret" not in str(body)


def test_database_down_is_503_with_retry_after(api_key):
    from unittest.mock import patch

    async def down(*a, **k):
        raise ConnectionRefusedError("no db")

    with patch("app.main.process_input", down):
        r = client.post("/extract", json={"url": "acme.com"}, headers=h(api_key))
    check(r, 503, "service_unavailable")
    assert r.headers["retry-after"] == "10"


def test_a_caller_request_id_is_echoed_and_a_bad_one_replaced(api_key):
    r = client.get("/nope", headers={**h(api_key), "X-Request-ID": "portal-123"})
    assert r.headers["x-request-id"] == "portal-123" == r.json()["request_id"]
    r = client.get("/nope", headers={**h(api_key), "X-Request-ID": "bad id\n<script>"})
    assert r.headers["x-request-id"].startswith("req_")


def test_success_responses_carry_a_request_id_too():
    r = client.get("/healthz")
    assert r.status_code == 200 and r.headers["x-request-id"].startswith("req_")


def test_every_code_has_a_status_and_description():
    for code, (status, when) in ERROR_CODES.items():
        assert 400 <= status < 600 and when, code


def test_preview_follows_a_chosen_column(api_key):
    csv_ = b"Name,Website,Email\nAcme,acme.com,mike@other.com\nBob,bob.com,x@y.com\n"
    r = client.post("/jobs/preview", files={"file": ("x.csv", csv_, "text/csv")},
                    data={"column": "Name"}, headers=h(api_key))
    assert r.status_code == 200
    body = r.json()
    assert body["column"] == "Name" and body["detected_column"] == "Website"
    assert all(s["status"] == "invalid_input" for s in body["samples"])
    check(client.post("/jobs/preview", files={"file": ("x.csv", csv_, "text/csv")},
                      data={"column": "Nope"}, headers=h(api_key)), 422, "unknown_column")
