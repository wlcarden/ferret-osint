"""Tests for input validation and normalization."""

import pytest

from osint_agent.input_validation import (
    InputValidationError,
    NORMALIZERS,
    normalize_domain,
    normalize_email,
    normalize_input,
    normalize_ip,
    normalize_name,
    normalize_phone,
    normalize_url,
    normalize_username,
)


# ------------------------------------------------------------------
# Phone
# ------------------------------------------------------------------

def test_phone_strips_formatting():
    assert normalize_phone("+1 (555) 867-5309") == "+15558675309"


def test_phone_strips_dots():
    assert normalize_phone("555.867.5309") == "5558675309"


def test_phone_preserves_plus():
    assert normalize_phone("+442071234567") == "+442071234567"


def test_phone_no_plus():
    assert normalize_phone("5558675309") == "5558675309"


def test_phone_too_short():
    with pytest.raises(InputValidationError, match="too short"):
        normalize_phone("123")


def test_phone_too_long():
    with pytest.raises(InputValidationError, match="too long"):
        normalize_phone("+1234567890123456")


def test_phone_error_attributes():
    with pytest.raises(InputValidationError) as exc_info:
        normalize_phone("12")
    assert exc_info.value.input_type == "phone"
    assert exc_info.value.value == "12"


# ------------------------------------------------------------------
# Domain
# ------------------------------------------------------------------

def test_domain_strips_protocol():
    assert normalize_domain("https://example.com") == "example.com"
    assert normalize_domain("http://example.com") == "example.com"


def test_domain_strips_www():
    assert normalize_domain("www.example.com") == "example.com"


def test_domain_strips_path_query_fragment():
    assert normalize_domain("https://www.example.com/page?q=1#top") == "example.com"


def test_domain_strips_port():
    assert normalize_domain("example.com:8080") == "example.com"


def test_domain_lowercases():
    assert normalize_domain("WWW.EXAMPLE.COM") == "example.com"


def test_domain_no_dot_raises():
    with pytest.raises(InputValidationError, match="not a valid domain"):
        normalize_domain("localhost")


def test_domain_empty_raises():
    with pytest.raises(InputValidationError):
        normalize_domain("   ")


def test_domain_invalid_chars():
    with pytest.raises(InputValidationError, match="invalid characters"):
        normalize_domain("exam ple.com")


# ------------------------------------------------------------------
# Email
# ------------------------------------------------------------------

def test_email_normalizes():
    assert normalize_email("  John.Doe@Example.COM  ") == "john.doe@example.com"


def test_email_invalid_no_at():
    with pytest.raises(InputValidationError, match="not a valid email"):
        normalize_email("notanemail")


def test_email_invalid_no_domain():
    with pytest.raises(InputValidationError, match="not a valid email"):
        normalize_email("user@")


def test_email_invalid_spaces():
    with pytest.raises(InputValidationError, match="not a valid email"):
        normalize_email("user @example.com")


# ------------------------------------------------------------------
# Name
# ------------------------------------------------------------------

def test_name_collapses_whitespace():
    assert normalize_name("  John   M.   Doe  ") == "John M. Doe"


def test_name_preserves_case():
    assert normalize_name("John Doe") == "John Doe"


def test_name_empty_raises():
    with pytest.raises(InputValidationError, match="empty name"):
        normalize_name("   ")


def test_name_too_short_raises():
    with pytest.raises(InputValidationError, match="too short"):
        normalize_name("A")


# ------------------------------------------------------------------
# Username
# ------------------------------------------------------------------

def test_username_strips_at():
    assert normalize_username("@johndoe") == "johndoe"


def test_username_strips_whitespace():
    assert normalize_username("  john_doe  ") == "john_doe"


def test_username_no_spaces():
    with pytest.raises(InputValidationError, match="cannot contain spaces"):
        normalize_username("john doe")


def test_username_empty_raises():
    with pytest.raises(InputValidationError, match="empty username"):
        normalize_username("@")


# ------------------------------------------------------------------
# URL
# ------------------------------------------------------------------

def test_url_adds_https():
    assert normalize_url("example.com/page") == "https://example.com/page"


def test_url_preserves_http():
    assert normalize_url("http://example.com/") == "http://example.com"


def test_url_strips_trailing_slash():
    assert normalize_url("https://example.com/") == "https://example.com"


def test_url_empty_raises():
    with pytest.raises(InputValidationError, match="empty URL"):
        normalize_url("   ")


# ------------------------------------------------------------------
# IP
# ------------------------------------------------------------------

def test_ip_v4():
    assert normalize_ip("  192.168.1.1  ") == "192.168.1.1"


def test_ip_v6():
    assert normalize_ip("2001:DB8::1") == "2001:db8::1"


def test_ip_v4_octet_overflow():
    with pytest.raises(InputValidationError, match="octet 256 exceeds 255"):
        normalize_ip("256.0.0.1")


def test_ip_invalid():
    with pytest.raises(InputValidationError, match="not a valid"):
        normalize_ip("not-an-ip")


def test_ip_empty():
    with pytest.raises(InputValidationError, match="empty IP"):
        normalize_ip("   ")


# ------------------------------------------------------------------
# normalize_input dispatcher
# ------------------------------------------------------------------

def test_normalize_input_routes_phone():
    assert normalize_input("phone", "+1-555-867-5309") == "+15558675309"


def test_normalize_input_routes_domain():
    assert normalize_input("domain", "https://www.example.com/page") == "example.com"


def test_normalize_input_routes_email():
    assert normalize_input("email", "  FOO@BAR.COM  ") == "foo@bar.com"


def test_normalize_input_routes_name():
    assert normalize_input("person_name", "  John   Doe  ") == "John Doe"


def test_normalize_input_routes_company():
    """company reuses normalize_name."""
    assert normalize_input("company", "  Acme   Corp  ") == "Acme Corp"


def test_normalize_input_unknown_type_strips():
    """Unknown types should just strip whitespace."""
    assert normalize_input("unknown_type", "  hello  ") == "hello"


# ------------------------------------------------------------------
# NORMALIZERS registry completeness
# ------------------------------------------------------------------

def test_normalizers_contains_expected_keys():
    expected = {"phone", "domain", "email", "person_name", "username", "url", "ip", "company", "name"}
    assert expected == set(NORMALIZERS.keys())
