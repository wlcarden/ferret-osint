"""Checking that quoted words really are in a text."""

MIN_QUOTE_LENGTH = 15


def squash(text: str) -> str:
    """Lower-case with runs of whitespace collapsed, for comparing quotes."""
    return " ".join(text.lower().split())


def quote_is_in(quote: str, text: str) -> bool:
    """True when the quote appears in the text and is long enough to mean something.

    A text shorter than the minimum can only be quoted whole.
    """
    needed = min(MIN_QUOTE_LENGTH, len(squash(text)))
    return len(squash(quote)) >= needed and squash(quote) in squash(text)
