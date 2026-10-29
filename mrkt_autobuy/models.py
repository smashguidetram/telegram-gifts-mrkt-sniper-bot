"""Domain models shared by client, rules and engine."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

NANO = 1_000_000_000


def nano_to_ton(value: int | float | str | None) -> float:
    if value in (None, ""):
        return 0.0
    return int(value) / NANO


def ton_to_nano(value: float) -> int:
    return int(round(value * NANO))


_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def norm(name: str | None) -> str:
    """Normalize a collection/model/backdrop/symbol name for comparison.

    "Lunar Snake", "lunar-snake" and "LunarSnake" all become "lunarsnake".
    """
    if not name:
        return ""
    return _NON_ALNUM.sub("", name.lower())


_SLUG_STRIP = re.compile(r"[^A-Za-z0-9]")


def nft_slug(collection: str | None, number: int | None) -> str | None:
    """t.me/nft slug: alnum collection name + '-' + number. TODO(verify) for unusual names."""
    if not collection or number is None:
        return None
    return f"{_SLUG_STRIP.sub('', collection)}-{number}"


def nft_url(collection: str | None, number: int | None) -> str | None:
    slug = nft_slug(collection, number)
    return f"https://t.me/nft/{slug}" if slug else None


@dataclass(slots=True)
class Listing:
    id: str
    collection: str
    model: str | None
    backdrop: str | None
    symbol: str | None
    number: int | None
    price_ton: float
    listed_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def key(self) -> str:
        """Dedupe key: a relist / price change of the same gift is a new event."""
        return f"{self.id}:{ton_to_nano(self.price_ton)}"

    @property
    def slug(self) -> str | None:
        return nft_slug(self.collection, self.number)

    @property
    def url(self) -> str | None:
        return nft_url(self.collection, self.number)

    @property
    def short_title(self) -> str:
        num = f" #{self.number}" if self.number is not None else ""
        return f"{self.collection}{num}"

    @property
    def title(self) -> str:
        num = f" #{self.number}" if self.number is not None else ""
        attrs = ", ".join(x for x in (self.model, self.backdrop, self.symbol) if x)
        return f"{self.collection}{num}" + (f" ({attrs})" if attrs else "")
