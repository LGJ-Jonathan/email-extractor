"""Step 1 smoke tests: settings load, schemas round-trip, auth is enforced."""

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import CSV_COLUMNS, DomainResult, to_csv_row

client = TestClient(app)


def test_healthz_is_open():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_extract_requires_api_key():
    assert client.post("/extract", json={"url": "acme.com"}).status_code == 401
    assert (
        client.post("/extract", json={"url": "acme.com"}, headers={"X-API-Key": "wrong"}).status_code
        == 401
    )


def test_extract_rejects_a_non_url_without_touching_the_network(api_key):
    """Normalisation short-circuits before DNS, so this must not make a request."""
    r = client.post(
        "/extract", json={"url": "Acme Roofing LLC"}, headers={"X-API-Key": api_key}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "invalid_input"
    assert body["error_reason"] == "not_a_url"
    assert body["best_email"] is None
    DomainResult.model_validate(body)


def test_extract_rejects_a_social_url(api_key):
    r = client.post(
        "/extract",
        json={"url": "facebook.com/PatriotFencingTX"},
        headers={"X-API-Key": api_key},
    )
    assert r.json()["error_reason"] == "social_or_directory"


CSV = (
    b"Company Name,Company Website,City\n"
    b"Acme Roofing,https://www.acme.com/contact,Dallas\n"
    b"Bob HVAC,bobshvac.com,Austin\n"
    b"Nameless,,Houston\n"
)


def test_upload_page_is_served_and_needs_no_key():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    for marker in ("Extractor", "/jobs/preview", "X-API-Key", "results.csv"):
        assert marker in r.text


def test_preview_detects_the_column_without_creating_a_job(api_key):
    r = client.post(
        "/jobs/preview",
        headers={"X-API-Key": api_key},
        files={"file": ("leads.csv", CSV, "text/csv")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["detected_column"] == "Company Website"
    assert body["confidence"] == "high"
    assert body["row_count"] == 3
    assert body["unique_domains"] == 2
    assert body["empty_website_count"] == 1
    assert body["columns"] == ["Company Name", "Company Website", "City"]
    assert len(body["samples"]) == 3


def test_preview_rejects_an_unparseable_file(api_key):
    r = client.post(
        "/jobs/preview",
        headers={"X-API-Key": api_key},
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    assert r.status_code == 422


def test_job_routes_require_the_api_key():
    files = {"file": ("leads.csv", CSV, "text/csv")}
    assert client.post("/jobs/preview", files=files).status_code == 401
    assert client.post("/jobs", files=files).status_code == 401
    assert client.get("/jobs/abc").status_code == 401
    assert client.get("/jobs/abc/results.csv").status_code == 401
    assert client.post("/jobs/abc/resume").status_code == 401


def test_csv_row_shapes():
    pending = to_csv_row(0, "acme.com", None)
    assert len(pending) == len(CSV_COLUMNS)
    assert pending[CSV_COLUMNS.index("status")] == "pending"

    result = DomainResult(
        domain="acme.com",
        final_domain="acme.com",
        status="found",
        best_email="mike@acme.com",
        best_email_source="rules",
        business_emails=["mike@acme.com", "info@acme.com"],
        phones=["+15551234567"],
        socials={"facebook": "https://facebook.com/acme"},
    )
    row = to_csv_row(7, "https://acme.com/contact", result)
    assert len(row) == len(CSV_COLUMNS)
    assert row[CSV_COLUMNS.index("best_email")] == "mike@acme.com"
    assert row[CSV_COLUMNS.index("business_emails")] == "mike@acme.com, info@acme.com"
    assert row[CSV_COLUMNS.index("facebook")] == "https://facebook.com/acme"
    assert row[CSV_COLUMNS.index("instagram")] == ""


def test_non_ascii_api_key_is_401_not_500():
    """compare_digest raises TypeError on non-ASCII str; headers decode as latin-1."""
    # Raw latin-1 byte: Starlette decodes headers as latin-1, so this reaches the
    # dependency as the str "\xe9", which compare_digest cannot compare as str.
    r = client.post("/extract", json={"url": "acme.com"}, headers={"X-API-Key": b"\xe9"})
    assert r.status_code == 401


def test_pending_row_keeps_the_known_domain():
    row = to_csv_row(3, "https://Acme.com/contact", None, domain="acme.com")
    assert row[CSV_COLUMNS.index("domain")] == "acme.com"
    assert row[CSV_COLUMNS.index("status")] == "pending"


def test_csv_formula_injection_is_neutralised():
    row = to_csv_row(1, "=cmd|'/C calc'!A1", None, domain="acme.com")
    assert row[CSV_COLUMNS.index("input_value")].startswith("'=")


def test_pending_is_not_a_valid_domain_result_status():
    import pydantic, pytest as _pytest

    with _pytest.raises(pydantic.ValidationError):
        DomainResult(domain="acme.com", status="pending")


def test_worker_settings_have_at_least_one_function():
    """arq refuses to construct a Worker with no functions and no cron jobs."""
    from app.worker import WorkerSettings

    assert WorkerSettings.functions
