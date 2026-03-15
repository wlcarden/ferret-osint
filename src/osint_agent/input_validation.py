"""Input validation and normalization for OSINT queries.

Validates and normalizes user inputs before they reach tool adapters.
Catches malformed inputs early to prevent wasted API calls and provides
user-friendly error messages.
"""

import re


class InputValidationError(ValueError):
    """Raised when input fails validation."""

    def __init__(self, input_type: str, value: str, reason: str):
        self.input_type = input_type
        self.value = value
        self.reason = reason
        super().__init__(f"Invalid {input_type} '{value}': {reason}")


def normalize_phone(phone: str) -> str:
    """Normalize a phone number to digits-only with optional leading +.

    Strips parentheses, dashes, spaces, dots. Preserves leading +
    for international format. Validates minimum length.

    >>> normalize_phone("+1 (555) 867-5309")
    '+15558675309'
    >>> normalize_phone("555.867.5309")
    '5558675309'
    """
    stripped = phone.strip()
    has_plus = stripped.startswith("+")
    digits = re.sub(r"[^\d]", "", stripped)

    if len(digits) < 7:
        raise InputValidationError(
            "phone", phone, "too short (need at least 7 digits)",
        )
    if len(digits) > 15:
        raise InputValidationError(
            "phone", phone, "too long (max 15 digits per E.164)",
        )

    return f"+{digits}" if has_plus else digits


def normalize_domain(domain: str) -> str:
    """Normalize a domain name by stripping protocol, www, paths, ports.

    >>> normalize_domain("https://www.example.com/page?q=1")
    'example.com'
    >>> normalize_domain("WWW.EXAMPLE.COM")
    'example.com'
    """
    d = domain.strip()

    # Strip protocol
    d = re.sub(r"^https?://", "", d, flags=re.IGNORECASE)
    # Strip www.
    d = re.sub(r"^www\.", "", d, flags=re.IGNORECASE)
    # Strip path, query, fragment
    d = d.split("/")[0].split("?")[0].split("#")[0]
    # Strip port
    d = d.split(":")[0]
    d = d.lower().strip(".")

    if not d or "." not in d:
        raise InputValidationError(
            "domain", domain, "not a valid domain (needs at least one dot)",
        )

    # Basic domain character validation
    if not re.match(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$", d):
        raise InputValidationError(
            "domain", domain,
            "contains invalid characters (only alphanumeric and hyphens allowed)",
        )

    return d


def normalize_email(email: str) -> str:
    """Normalize an email address: lowercase, strip whitespace.

    >>> normalize_email("  John.Doe@Example.COM  ")
    'john.doe@example.com'
    """
    e = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e):
        raise InputValidationError(
            "email", email, "not a valid email address format",
        )
    return e


def normalize_name(name: str) -> str:
    """Normalize a person or organization name.

    Collapses whitespace, strips leading/trailing whitespace.
    Does NOT change case (names go to APIs that may be case-sensitive).

    >>> normalize_name("  John   M.   Doe  ")
    'John M. Doe'
    """
    n = re.sub(r"\s+", " ", name.strip())
    if not n:
        raise InputValidationError("name", name, "empty name")
    if len(n) < 2:
        raise InputValidationError("name", name, "name too short (need at least 2 characters)")
    return n


def normalize_username(username: str) -> str:
    """Normalize a username/handle.

    Strips leading @, whitespace. Validates no spaces.

    >>> normalize_username("@johndoe")
    'johndoe'
    >>> normalize_username("  john_doe  ")
    'john_doe'
    """
    u = username.strip().lstrip("@")
    if not u:
        raise InputValidationError("username", username, "empty username")
    if " " in u:
        raise InputValidationError(
            "username", username, "usernames cannot contain spaces",
        )
    return u


def normalize_url(url: str) -> str:
    """Normalize a URL: add https:// if no scheme, strip trailing slash.

    >>> normalize_url("example.com/page")
    'https://example.com/page'
    >>> normalize_url("http://example.com/")
    'http://example.com'
    """
    u = url.strip()
    if not u:
        raise InputValidationError("url", url, "empty URL")

    # Add scheme if missing
    if not re.match(r"^https?://", u, re.IGNORECASE):
        u = f"https://{u}"

    # Strip trailing slash
    u = u.rstrip("/")
    return u


def normalize_ip(ip: str) -> str:
    """Validate and normalize an IP address (v4 or v6).

    >>> normalize_ip("  192.168.1.1  ")
    '192.168.1.1'
    """
    i = ip.strip()
    if not i:
        raise InputValidationError("ip", ip, "empty IP address")

    # Basic IPv4 validation
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", i):
        parts = i.split(".")
        for part in parts:
            if int(part) > 255:
                raise InputValidationError(
                    "ip", ip, f"octet {part} exceeds 255",
                )
        return i

    # Basic IPv6 validation (allow :: shorthand)
    if ":" in i and re.match(r"^[0-9a-fA-F:]+$", i):
        return i.lower()

    raise InputValidationError("ip", ip, "not a valid IPv4 or IPv6 address")


# Maps input type → normalizer function
NORMALIZERS = {
    "phone": normalize_phone,
    "domain": normalize_domain,
    "email": normalize_email,
    "person_name": normalize_name,
    "username": normalize_username,
    "url": normalize_url,
    "ip": normalize_ip,
    "company": normalize_name,
    "name": normalize_name,
}


def normalize_input(input_type: str, value: str) -> str:
    """Normalize an input value based on its detected type.

    Returns the normalized value. Raises InputValidationError on
    invalid inputs.

    If no normalizer exists for the input_type, returns the
    stripped value unchanged.
    """
    normalizer = NORMALIZERS.get(input_type)
    if normalizer:
        return normalizer(value)
    return value.strip()
