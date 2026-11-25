"""Checks done by code on untrusted text, with no model involved.

Both are deliberately crude. They exist so that two failures of a language
model are noticed by something that cannot be talked out of it: obeying an
instruction planted in the text, and overlooking a violent statement.

Text is checked the way a reader is shown it, with every run of spaces and
line breaks as one space. Otherwise wording split across two lines would pass
the check and then be shown to the reader on one.
"""

import re

# Text that addresses an automated reader. A match is flagged, never obeyed.
INSTRUCTION_PATTERN = re.compile(
    r"ignore (all |any |your |the )?(previous|prior|above|earlier) instructions"
    r"|system (notice|prompt|message|override)"
    r"|\b(ai|llm) (agents?|assistants?|models?)\b"
    r"|\bautomated (reviewer|reader|review system)\b"
    r"|\b(any|an|the) (ai|llm|language model|chatbot) (reading|reviewing|processing)\b"
    r"|do not (mention|reveal|report) this"
    r"|\b(record_judgement|verify_claims|render_brief|dismiss_evidence|run_source"
    r"|read_posts|submit_review)\b",
    re.IGNORECASE,
)

# Words that often appear in statements about violence. Many harmless posts
# match and some threats do not, so a match only earns a post another reading.
WATCH_WORDS = re.compile(
    r"\b(kill\w*|murder\w*|shoot\w*|shot|stab\w*|hang(ed|ing)?|lynch\w*|burn\w*|bomb\w*|"
    r"bl(ow|ew)\w* up|explo\w*|beat(ing|en)?|punch\w*|strangl\w*|chok\w*|slaughter\w*|"
    r"massacre\w*|execut\w*|behead\w*|bur(y|ied)|dead|die[sd]?|dying|death|blood\w*|"
    r"bullet\w*|rope|grave|body bag|run over|ran over|break (his|her|their|your|both)|"
    r"hurt\w*|hunt (him|her|them|you) down|where you (live|work)|what's coming|"
    r"in the ground|six feet under)\b",
    re.IGNORECASE,
)


def _as_shown(text: str) -> str:
    return " ".join(text.split())


def addresses_a_reader(text: str) -> bool:
    """True when the text contains wording aimed at an automated reader."""
    return bool(INSTRUCTION_PATTERN.search(_as_shown(text)))


def has_watch_words(text: str) -> bool:
    """True when the text contains a word that often goes with violence."""
    return bool(WATCH_WORDS.search(_as_shown(text)))
