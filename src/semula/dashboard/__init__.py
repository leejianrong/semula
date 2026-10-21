"""The dashboard, built on indah and mounted at ``/ui`` (ADR-0006).

It is one more surface over the service layer (ADR-0005): in-process, so no HTTP hop and no
second copy of the rules. Only ``mount`` is imported at start-up; indah itself (about 0.4 s to
import) loads on the first request to ``/ui``, so the cold start budget is untouched.
"""
