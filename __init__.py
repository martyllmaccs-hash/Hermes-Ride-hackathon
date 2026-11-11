"""hermes-ride: hands-free motorcycle voice platform for the Hermes gateway.

Kept light on purpose: the adapter (aiohttp server, Scribe client) is imported only when Hermes calls
``register(ctx)``.
"""


def register(ctx):
    from .adapter import register as _register
    _register(ctx)
