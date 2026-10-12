"""pf_runtime — the tested library every generated Fivetran Connector SDK connector runs.

It is vendored (copied) into each connector package at build time, pinned by version, and driven
only by the locked spec (spec.lock.json). No LLM is ever called at runtime.
"""
VERSION = "0.1.0"
