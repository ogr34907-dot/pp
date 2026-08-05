from __future__ import annotations

import io
from pathlib import Path
import token
import tokenize


ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRECTORIES = (
    "application",
    "domain",
    "engine",
    "infrastructure",
    "interfaces",
    "scripts",
)


def test_production_source_does_not_call_deprecated_datetime_utcnow():
    deprecated_calls: list[str] = []

    for directory in SOURCE_DIRECTORIES:
        for source_file in (ROOT / directory).rglob("*.py"):
            source_tokens = [
                source_token
                for source_token in tokenize.tokenize(
                    io.BytesIO(source_file.read_bytes()).readline
                )
                if source_token.type
                not in {
                    token.ENCODING,
                    token.COMMENT,
                    token.DEDENT,
                    token.ENDMARKER,
                    token.INDENT,
                    token.NEWLINE,
                    token.NL,
                }
            ]
            for index, source_token in enumerate(source_tokens[:-3]):
                if source_token.string != "datetime":
                    continue
                if source_tokens[index + 1].string != ".":
                    continue
                if source_tokens[index + 2].string != "utcnow":
                    continue
                if source_tokens[index + 3].string == "(":
                    deprecated_calls.append(
                        f"{source_file.relative_to(ROOT)}:{source_token.start[0]}"
                    )

    assert not deprecated_calls, (
        "Production code must use timezone-aware UTC timestamps instead of "
        f"datetime.utcnow(): {', '.join(deprecated_calls)}"
    )
