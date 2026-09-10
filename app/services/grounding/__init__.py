"""Grounding: making "every agent explains itself" true rather than intended.

The claim is already being made in client conversations. This package is the
mechanism behind it, and every module in it fails closed:

- `retrieval.py` — one permission-scoped path to grounding content, and a
  forbidden-source list that no agent can reach around.
- `citations.py` — a claim cites a record, or the response is withheld.
- `refusals.py` — the four things an HR agent must decline, and how to decline
  the advisory half of a question while answering the factual half.
- `confidence.py` — below a threshold, "insufficient signal" rather than a weak
  score.
- `injection.py` — employee-supplied text is untrusted input, delimited and
  checked.

Everything except `service.py` is pure.
"""
