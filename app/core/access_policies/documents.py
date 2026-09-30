"""Letters and the document vault."""

ENDPOINTS: dict[str, str] = {
    "GET /letter-templates": (
        "reference: the template catalogue, not issued letters. Inactive versions "
        "only for HR."
    ),
    "GET /letter-templates/merge-fields": "reference: the merge tokens a template may use.",
    "GET /letters": "permissions: your own letters unless HR.",
    "GET /letters/signature-provider": (
        "reference: whether an e-signature provider is connected, about nobody in particular."
    ),
    "GET /letters/{letter_id}/pdf": "permissions: your own letter unless HR.",
    "GET /letters/{letter_id}/verify": "permissions: your own letter unless HR.",
    "GET /letters/{letter_id}/content": (
        "permissions: your own letter unless HR; rendered only in a sandboxed frame."
    ),
    "POST /letters/{letter_id}/acknowledge": (
        "permissions: only the employee the letter was issued to (require_self) - HR cannot "
        "acknowledge on their behalf."
    ),
    "GET /documents/categories": "reference: the vault's document categories.",
    "GET /documents/categories/self-upload": (
        "reference: which categories an employee may upload themselves."
    ),
    "POST /documents/mine": (
        "own: a document added to the caller's own record, pending HR "
        "verification; the owner comes from the login, never the form."
    ),
    "GET /documents": (
        "permissions: HR sees every vault; everybody else only their own documents "
        "marked employee-visible."
    ),
    "GET /documents/{document_id}/download": (
        "permissions: HR, or the employee themselves for a document marked visible; "
        "anything else is a 404."
    ),
}
