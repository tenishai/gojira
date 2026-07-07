"""The sizing feedback loop: usage evidence in, rightsizing proposals out.

See docs/sizing-feedback-design.md. Three seams keep the core dependency-clean:
the /api/v1/usage ingestion endpoint, the SizingStore, and the Recommender —
each with a no-dependency default; richer backends plug in from outside.
"""
