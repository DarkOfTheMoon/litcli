"""Command implementations, one module per intent family.

``litcli.cli`` keeps only the argument parsing entry point, the proxy setup and the
dispatch; each family lives here so a feature (search, graph edges, store CRUD, export,
fulltext/book fetch, diagnostics) can be read and changed on its own.

- ``parser``: the argparse surface (intent-based subcommands, shared backend flag).
- ``render``: item presentation shared by the read commands.
- ``keys``: item-key normalization shared by export and the graph edges.
- ``search``, ``graph``, ``data``, ``export``, ``fetch``, ``diagnostics``: the commands.

Tests patch backend helpers on the module that uses them (e.g.
``litcli.commands.data.get_backend``), not on ``litcli.cli``.
"""
