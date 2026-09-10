"""Operational security: the posture an enterprise buyer verifies before signing.

Procurement asks for RPO and RTO numbers, a pen-test summary and SIEM
integration, and each is a hard stop rather than a negotiable point.

This package builds the parts that are application code — the event schema and
its delivery health, session and device policy, step-up authentication, IP
allowlisting, and the registers that record drills, runbooks and findings. The
drills themselves are operational work and are recorded here rather than
performed here; every claim in the published posture says which it is.

Everything except `service.py` is pure.
"""
