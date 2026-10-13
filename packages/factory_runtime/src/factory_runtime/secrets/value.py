"""SecretValue: a secret that cannot be printed, logged, pickled, copied or serialised by accident.

The value is registered with the log redactor the moment it is created (fetch time), together with the forms it
commonly takes on the wire (JSON-escaped, URL-encoded, base64), so even an accidental log line is masked.
`.reveal()` hands out the plain value only inside a `with` block.
"""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import Iterator
from typing import Any, NoReturn
from urllib.parse import quote, quote_plus

from factory_core.errors import ConfigError
from factory_core.jsonlog import register_secret


def register_forms(value: str) -> None:
    """Register a secret and its usual encodings with the log redactor."""
    register_secret(value)
    register_secret(quote_plus(value))
    register_secret(quote(value, safe=""))
    register_secret(base64.b64encode(value.encode()).decode())


class SecretValue:
    __slots__ = ("_name", "_value")
    _name: str
    _value: str

    def __init__(self, value: str, name: str = "secret") -> None:
        if not isinstance(value, str):
            raise TypeError("a secret value is text")
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_name", name)
        register_forms(value)

    @property
    def name(self) -> str:
        return str(self._name)

    def __repr__(self) -> str:
        return f"SecretValue({self._name}, ***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __setattr__(self, key: str, value: Any) -> NoReturn:
        raise AttributeError("SecretValue is immutable")

    def _refuse(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError("secret values cannot be pickled, copied or serialised")

    __reduce__ = _refuse
    __reduce_ex__ = _refuse
    __getstate__ = _refuse
    __copy__ = _refuse
    __deepcopy__ = _refuse

    @contextlib.contextmanager
    def reveal(self) -> Iterator[str]:
        yield str(self._value)

    def json_fields(self) -> dict[str, SecretValue]:
        """For secrets stored as a JSON object (e.g. an OAuth client): one SecretValue per field."""
        try:
            doc = json.loads(self._value)
        except json.JSONDecodeError:
            raise ConfigError("secret_format_invalid", f"secret '{self._name}' is not a JSON object") from None
        if not isinstance(doc, dict):
            raise ConfigError("secret_format_invalid", f"secret '{self._name}' is not a JSON object")
        return {str(k): SecretValue(str(v), f"{self._name}.{k}") for k, v in doc.items()}
