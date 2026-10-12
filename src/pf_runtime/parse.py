"""Streaming readers for package members: CSV, JSON Lines, JSON array."""
from __future__ import annotations

import codecs
import csv
import io
import json
from typing import Iterator

from .errors import PackageRejected


class Reader:
    """Iterate (row_number, {source_column: raw_value}) and expose the header."""

    def __init__(self, path: str, fmt: dict, expected_columns: list[str]):
        self.path = path
        self.fmt = fmt
        self.expected = expected_columns
        self.header: list[str] = []

    def rows(self) -> Iterator[tuple[int, dict]]:
        t = self.fmt.get("type", "csv")
        if t == "csv":
            yield from self._csv()
        elif t == "jsonl":
            yield from self._jsonl()
        else:
            yield from self._json()

    def _text(self):
        enc = self.fmt.get("encoding", "utf-8")
        if codecs.lookup(enc).name == "utf-8":  # any spelling (utf8, UTF-8, utf_8)
            enc = "utf-8-sig"  # tolerate a byte-order mark
        return io.open(self.path, "r", encoding=enc, newline="")

    def _csv(self) -> Iterator[tuple[int, dict]]:
        nulls = set(self.fmt.get("null_tokens", [""]))
        csv.field_size_limit(64 * 1024 * 1024)
        try:
            with self._text() as fh:
                rd = csv.reader(fh, delimiter=self.fmt.get("delimiter", ","), quotechar=self.fmt.get("quotechar", '"'), strict=True)
                if self.fmt.get("header", True):
                    try:
                        self.header = [h.strip() for h in next(rd)]
                    except StopIteration:
                        self.header = []
                        return
                    if len(set(self.header)) != len(self.header):
                        raise PackageRejected("csv_header", "duplicate column names in the header")
                else:
                    self.header = list(self.expected)
                n_cols = len(self.header)
                for i, rec in enumerate(rd, start=1):
                    if n_cols == 1 and not rec:
                        rec = [""]  # one-column file: an empty line is an empty (null) value, not a blank line
                    elif not rec or (len(rec) == 1 and rec[0] == ""):
                        continue  # blank line
                    if len(rec) != n_cols:
                        yield i, {"__pf_malformed__": True}
                        continue
                    yield i, {h: (None if v in nulls else v) for h, v in zip(self.header, rec)}
        except UnicodeDecodeError:
            raise PackageRejected("encoding", "member is not in the declared encoding") from None
        except csv.Error:
            raise PackageRejected("csv_format", "member is not well-formed CSV") from None

    def _jsonl(self) -> Iterator[tuple[int, dict]]:
        seen: list[str] = []
        try:
            with self._text() as fh:
                for i, line in enumerate(fh, start=1):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        yield i, {"__pf_malformed__": True}
                        continue
                    if not isinstance(obj, dict):
                        yield i, {"__pf_malformed__": True}
                        continue
                    for k in obj:
                        if k not in seen:
                            seen.append(k)
                    self.header = seen
                    yield i, obj
        except UnicodeDecodeError:
            raise PackageRejected("encoding", "member is not in the declared encoding") from None

    def _json(self) -> Iterator[tuple[int, dict]]:
        try:
            with self._text() as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise PackageRejected("json_format", "member is not valid JSON") from None
        if not isinstance(data, list):
            raise PackageRejected("json_format", "JSON member must be an array of objects")
        seen: list[str] = []
        for i, obj in enumerate(data, start=1):
            if not isinstance(obj, dict):
                yield i, {"__pf_malformed__": True}
                continue
            for k in obj:
                if k not in seen:
                    seen.append(k)
            self.header = seen
            yield i, obj
