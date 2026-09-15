"""Letters and the document vault."""

ENDPOINTS: dict[str, str] = {
    "GET /letter-templates": "reference: the template catalogue, not issued letters.",
    "GET /letters": "permissions: your own letters unless HR.",
    "GET /letters/{letter_id}/pdf": "permissions: your own letter unless HR.",
    "GET /letters/{letter_id}/verify": "permissions: your own letter unless HR.",
}
