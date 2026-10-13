"""Secret provider (ADR-0008): references in specs, values only at run time, never logged or persisted.

Stack per task: Audited(Cached(Router(Vault | AWS Secrets Manager))). Values are SecretValue objects: masked in every
representation and registered with the log redactor when fetched.
"""

from .provider import ResolvedRef, SecretProvider, resolve_ref
from .value import SecretValue, register_forms

__all__ = ["ResolvedRef", "SecretProvider", "SecretValue", "register_forms", "resolve_ref"]
