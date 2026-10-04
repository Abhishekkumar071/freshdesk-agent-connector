import pytest
from pydantic import ValidationError

from freshdesk_connector.config import Settings

FAKE_KEY = "fake-key-should-never-leak-123"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "FRESHDESK_DOMAIN",
        "FRESHDESK_API_KEY",
        "FRESHDESK_TIMEOUT_SECONDS",
        "FRESHDESK_MAX_ATTEMPTS",
        "FRESHDESK_MAX_RETRY_WAIT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def make(**overrides) -> Settings:
    values = {"domain": "acme", "api_key": FAKE_KEY} | overrides
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("acme", "acme.freshdesk.com"),
        ("acme.freshdesk.com", "acme.freshdesk.com"),
        ("  ACME.Freshdesk.com ", "acme.freshdesk.com"),
        ("my-shop-2", "my-shop-2.freshdesk.com"),
    ],
)
def test_domain_is_normalized(raw, expected):
    settings = make(domain=raw)
    assert settings.domain == expected
    assert settings.base_url == f"https://{expected}/api/v2"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "https://acme.freshdesk.com",
        "acme.freshdesk.com/api",
        "acme.freshdesk.com:8443",
        "evil.com",
        "acme.freshdesk.com.evil.com",
        "support.acme.com",
        "a.b.freshdesk.com",
        "freshdesk.com",
        "-acme",
        "acme_shop",
    ],
)
def test_non_freshdesk_domains_are_rejected(raw):
    with pytest.raises(ValidationError):
        make(domain=raw)


def test_reads_from_environment(monkeypatch):
    monkeypatch.setenv("FRESHDESK_DOMAIN", "acme")
    monkeypatch.setenv("FRESHDESK_API_KEY", FAKE_KEY)
    monkeypatch.setenv("FRESHDESK_MAX_ATTEMPTS", "2")

    settings = Settings(_env_file=None)

    assert settings.domain == "acme.freshdesk.com"
    assert settings.api_key.get_secret_value() == FAKE_KEY
    assert settings.max_attempts == 2


def test_defaults():
    settings = make()
    assert settings.timeout_seconds == 10.0
    assert settings.max_attempts == 3
    assert settings.max_retry_wait_seconds == 10.0


def test_missing_api_key_names_the_variable():
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, domain="acme")
    assert "api_key" in str(excinfo.value)


def test_empty_api_key_rejected():
    with pytest.raises(ValidationError):
        make(api_key="")


@pytest.mark.parametrize(
    "overrides",
    [{"timeout_seconds": 0}, {"max_attempts": 0}, {"max_attempts": 6}, {"max_retry_wait_seconds": -1}],
)
def test_out_of_range_tuning_rejected(overrides):
    with pytest.raises(ValidationError):
        make(**overrides)


def test_api_key_not_in_repr_or_str():
    settings = make()
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)
    assert FAKE_KEY not in str(settings.model_dump())


def test_validation_errors_do_not_echo_input():
    with pytest.raises(ValidationError) as excinfo:
        make(domain=f"{FAKE_KEY}.evil.com")
    assert FAKE_KEY not in str(excinfo.value)
