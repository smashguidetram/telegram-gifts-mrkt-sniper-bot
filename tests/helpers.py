"""Shared offline test helpers (no network, no credentials)."""

from __future__ import annotations

from dataclasses import replace

from mrkt_autobuy.client import DEMO_RULES, MockClient
from mrkt_autobuy.config import Settings
from mrkt_autobuy.engine import Engine
from mrkt_autobuy.models import Listing
from mrkt_autobuy.notifier import MemoryNotifier
from mrkt_autobuy.storage import Storage

ADMIN = 42
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def settings(**overrides) -> Settings:
    base = dict(_env_file=None, DRY_RUN=True, MAX_PRICE_TON=10, DAILY_BUDGET_TON=50, MAX_BUYS_PER_HOUR=3,
                POLL_INTERVAL=0.01, PAGE_SIZE=20, ADMIN_IDS=str(ADMIN))
    base.update(overrides)
    return Settings(**base)


def L(lid: str, price: float, collection: str = "Lunar Snake", model: str | None = "Albino", number: int | None = 1,
      **raw) -> Listing:
    """A listing as client.parse_listing builds it: the price came from salePrice (nanoTON)."""
    raw.setdefault("salePrice", round(price * 1e9))
    return Listing(id=lid, collection=collection, model=model, backdrop=None, symbol=None, number=number,
                   price_ton=price, raw=raw)


async def demo_engine(s: Settings | None = None, *, rules=None, force_dry: bool = True, clock: Clock | None = None,
                      client: MockClient | None = None, **kw):
    s = s or settings()
    clock = clock or Clock()
    storage = await Storage(":memory:").open()
    for rule in (DEMO_RULES if rules is None else rules):
        await storage.add_rule(replace(rule, id=None))
    client = client or MockClient("demo", now=clock())
    notifier = MemoryNotifier()
    engine = Engine(s, storage, client, notifier, force_dry=force_dry, clock=clock, **kw)
    return engine, storage, client, notifier, clock
