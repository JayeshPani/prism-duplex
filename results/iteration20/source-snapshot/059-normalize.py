"""Deterministic transcript helpers applied before the LLM sees an utterance.

Speech-to-text writes spelled identifiers as separate tokens ("A B C one two
three", "K as in kilo 4 4 9"). Small LLMs often join those wrongly, so we
detect runs of spelled characters and give the planner the joined form as a
hint next to the original words.
"""

from __future__ import annotations

import re

_NUM = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_TOKEN = re.compile(r"[A-Za-z]+|\d+|[-]")


def _spelled(tok: str) -> str | None:
    t = tok.lower()
    if t in _NUM and t not in ("o", "oh"):
        return _NUM[t]
    if tok.isdigit() and len(tok) <= 4:
        return tok
    if len(tok) == 1 and tok.isalpha():
        return tok.upper()
    return None


def spelled_runs(text: str) -> list[tuple[str, str]]:
    """Return (original span, joined form) for each spelled run of >= 3 characters."""
    out: list[tuple[str, str]] = []
    words = list(re.finditer(r"\S+", text))
    i = 0
    while i < len(words):
        j, parts, pending_double, n_pieces = i, [], False, 0
        while j < len(words):
            raw = words[j].group().strip(",.;:!?\"'()")
            low = raw.lower()
            if low in ("double", "triple") and j + 1 < len(words):
                pending_double = 2 if low == "double" else 3
                j += 1
                continue
            if low in ("as", "in") or (low.isalpha() and len(low) > 1 and parts and words[j - 1].group().lower() == "in"):
                # "K as in kilo" -> keep K, skip the phonetic word
                if parts and low in ("as", "in"):
                    j += 1
                    continue
                if parts and words[j - 1].group().lower() == "in":
                    j += 1
                    continue
            pieces = [p for p in re.split(r"-", raw) if p]
            mapped = [_spelled(p) for p in pieces] if pieces else [None]
            if not pieces or any(m is None for m in mapped):
                break
            joined = "".join(mapped)
            n_pieces += len(pieces)
            parts.append(joined * (pending_double or 1))
            pending_double = False
            j += 1
        chars = "".join(parts)
        two_piece_code = (n_pieces == 2 and len(parts) == 2 and parts[0].isalpha() and parts[1].isdigit()
                          and words[i].group().strip(",.") not in ("I", "a", "A"))
        if n_pieces >= 3 or two_piece_code:
            if any(c.isdigit() for c in chars) or n_pieces >= 4:
                span = text[words[i].start():words[j - 1].end()]
                out.append((span, chars))
            i = j
        else:
            i += 1
    return out


def planner_hints(text: str) -> str:
    runs = spelled_runs(text)
    if not runs:
        return ""
    return "Spelled sequences detected (use the joined form for ids/numbers): " + "; ".join(
        f'"{span}" -> "{joined}"' for span, joined in runs)
