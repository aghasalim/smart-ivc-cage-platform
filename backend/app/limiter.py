"""Shared rate-limiter instance.

Lives in its own module so individual route files can decorate their routes
with `@limiter.limit(...)` without creating a circular import with main.py.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
