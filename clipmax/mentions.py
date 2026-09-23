"""Detección de menciones entre streamers y de palabras de 'hype' en texto.

Funciona igual para mensajes de chat y para transcripciones de whisper:
- Normaliza (minúsculas, sin tildes, sin @, emotes de Kick -> su nombre).
- Busca cada alias como palabra completa ("west" no debe saltar con "western").
- Tolera errores de transcripción con una comparación difusa por palabra
  (whisper puede escribir "Wescol", "Güescol", "Gir of nos"...).
"""

from __future__ import annotations

import difflib
import re
import unicodedata

_EMOTE_RE = re.compile(r"\[emote:\d+:([^\]]+)\]")
_NON_WORD = re.compile(r"[^\w\s?!]+", re.UNICODE)
_SPACES = re.compile(r"\s+")
_LAUGH_RE = re.compile(r"(?:ja|je|ji|js|sj|kj|jk)+[jas]?|x+d+|lo+l|lma+o+|ksks\w*")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def normalize(text: str) -> str:
    text = _EMOTE_RE.sub(lambda m: f" {m.group(1)} ", text or "")
    text = strip_accents(text.lower()).replace("@", " ")
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def emote_names(text: str) -> list[str]:
    return [m.lower() for m in _EMOTE_RE.findall(text or "")]


class MentionMatcher:
    """Encuentra qué streamers se mencionan en un texto."""

    def __init__(self, streamers: list[dict], fuzzy_ratio: float = 0.86):
        self.fuzzy_ratio = fuzzy_ratio
        self._patterns: dict[str, re.Pattern] = {}
        self._fuzzy: dict[str, list[str]] = {}
        for s in streamers:
            aliases = sorted({normalize(a) for a in s.get("alias", []) + [s["slug"], s["nombre"]] if a},
                             key=len, reverse=True)
            aliases = [a for a in aliases if a]
            if not aliases:
                continue
            alt = "|".join(re.escape(a).replace(r"\ ", r"\s+") for a in aliases)
            self._patterns[s["slug"]] = re.compile(rf"(?<!\w)(?:{alt})(?!\w)")
            # La comparación difusa solo para alias de una palabra y 5+ letras
            # (con alias cortos como "west" daría demasiados falsos positivos).
            self._fuzzy[s["slug"]] = [a for a in aliases if " " not in a and len(a) >= 5]

    def find(self, text: str, fuzzy: bool = False) -> dict[str, int]:
        """{slug: número de menciones} en el texto."""
        norm = normalize(text)
        found: dict[str, int] = {}
        if not norm:
            return found
        for slug, pat in self._patterns.items():
            n = len(pat.findall(norm))
            if n:
                found[slug] = n
        if fuzzy:
            words = set(norm.split())
            for slug, aliases in self._fuzzy.items():
                if slug in found:
                    continue
                for w in words:
                    if len(w) < 4:
                        continue
                    if any(difflib.SequenceMatcher(None, w, a).ratio() >= self.fuzzy_ratio for a in aliases):
                        found[slug] = found.get(slug, 0) + 1
        return found

    def find_others(self, text: str, own_slug: str, fuzzy: bool = False) -> dict[str, int]:
        return {k: v for k, v in self.find(text, fuzzy=fuzzy).items() if k != own_slug}


class HypeDetector:
    """Cuenta si un mensaje de chat expresa reacción fuerte (risas, sorpresa, 'clip')."""

    def __init__(self, words: list[str]):
        self.tokens = {normalize(w) for w in words if normalize(w)}
        self.symbols = {w for w in words if not normalize(w)}  # emojis, "?????"

    def is_hype(self, text: str) -> bool:
        raw = text or ""
        if any(sym in raw for sym in self.symbols):
            return True
        if "???" in raw or "!!!" in raw:
            return True
        norm = normalize(raw)
        words = norm.split()
        if any(w in self.tokens for w in words):
            return True
        # Risas: jajaja, jsjsjs, kjkj, xdd, lmao...
        if any(_LAUGH_RE.fullmatch(w) for w in words):
            return True
        # Mensajes en mayúsculas sostenidas (gritos del chat).
        letters = [c for c in raw if c.isalpha()]
        return len(letters) >= 6 and sum(c.isupper() for c in letters) / len(letters) > 0.8
