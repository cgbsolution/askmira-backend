"""Human-in-the-loop configuration: what agents may do on their own.

"Agents propose and humans approve" is currently a hardcoded behaviour and an
implicit promise. This package makes it a setting a tenant configures and can
see — with a platform ceiling they cannot raise, because some things must never
be automatic whatever a client asks for.

Everything except `service.py` is pure.
"""
