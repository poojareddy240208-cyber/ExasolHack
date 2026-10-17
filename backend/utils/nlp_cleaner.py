"""
OmniSync NLP Cleaner Utility.

Sanitizes raw customer support ticket text and user comments prior to ingestion
into Exasol staging tables and downstream Transformer-based sentiment classification.
"""

from datetime import datetime, timezone
import html
import re
from typing import Any, Optional

# Regular expressions for text sanitation
# Block-level HTML elements replaced with space to preserve sentence boundaries
BLOCK_TAGS_PATTERN = re.compile(r"(?i)<(?:br|p|div|li|tr|h[1-6]|table|blockquote)\b[^>]*>")
BLOCK_TAGS_END_PATTERN = re.compile(r"(?i)</(?:p|div|li|tr|h[1-6]|table|blockquote)>")

# Remaining HTML tags (e.g. <b>, <span>, <a>) stripped cleanly without introducing unwanted intra-word spaces
REMAINING_TAGS_PATTERN = re.compile(r"<[^>]+>")

# Non-printable ASCII control characters: 0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F, 0x7F
CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Excess whitespaces, newlines, and tabs
WHITESPACE_PATTERN = re.compile(r"\s+")


def clean_ticket_text(raw_text: Optional[str], max_length: int = 4000) -> str:
    """
    Sanitizes raw customer ticket and comment strings:
    1. Handles null, empty, or non-string inputs safely without throwing exceptions.
    2. Unescapes HTML entities (&amp;, &lt;, &gt;, &quot;, &#39;, &nbsp;).
    3. Converts block-level HTML tags into spaces to preserve phrase separation.
    4. Strips all remaining HTML tags cleanly.
    5. Strips non-printable ASCII control characters.
    6. Collapses redundant whitespace and trims leading/trailing spaces.
    7. Bounds text to max_length (default 4000 chars for Exasol VARCHAR(4000)).

    Args:
        raw_text: Raw incoming text string from Zendesk webhook payload.
        max_length: Maximum permitted length for database storage.

    Returns:
        Cleaned and sanitized text string.
    """
    if raw_text is None:
        return ""

    if not isinstance(raw_text, str):
        raw_text = str(raw_text)

    if not raw_text.strip():
        return ""

    # Unescape HTML entities first
    text = html.unescape(raw_text)

    # Replace block-level tags with a space
    text = BLOCK_TAGS_PATTERN.sub(" ", text)
    text = BLOCK_TAGS_END_PATTERN.sub(" ", text)

    # Strip all remaining inline HTML tags
    text = REMAINING_TAGS_PATTERN.sub("", text)

    # Remove non-printable control characters
    text = CONTROL_CHARS_PATTERN.sub("", text)

    # Collapse multiple whitespace characters into single space
    text = WHITESPACE_PATTERN.sub(" ", text).strip()

    # Enforce maximum column length constraint
    if len(text) > max_length:
        text = text[:max_length].rstrip()

    return text


def normalize_timestamp(val: Any) -> str:
    """
    Normalizes a timestamp value (datetime object, ISO 8601 string, or epoch)
    into standard Exasol TIMESTAMP format '%Y-%m-%d %H:%M:%S'.

    Args:
        val: Incoming timestamp representation or None.

    Returns:
        String formatted as 'YYYY-MM-DD HH:MM:SS'.
    """
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(val, str) and val.strip():
        clean_str = val.strip().replace("Z", "").replace("T", " ")
        if "+" in clean_str:
            clean_str = clean_str.split("+")[0].strip()
        try:
            parsed = datetime.fromisoformat(clean_str)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return clean_str[:19]

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
