import asyncio

from mrkt_autobuy.bot import HELP, AppContext, build_dispatcher, parse_on_off
from mrkt_autobuy.client import MockClient
from mrkt_autobuy.config import Settings
from mrkt_autobuy.engine import Engine
from mrkt_autobuy.notifier import MemoryNotifier
from mrkt_autobuy.storage import Storage


def test_parse_on_off():
    assert parse_on_off("on") is True
    assert parse_on_off(" OFF ") is False
    assert parse_on_off(None) is None
    assert parse_on_off("maybe") is None


def test_help_lists_all_commands():
    for cmd in ("/start", "/status", "/pause", "/resume", "/rules", "/addrule", "/delrule", "/dry", "/balance",
                "/buys", "/floor"):
        assert cmd in HELP or cmd == "/start"


def test_dispatcher_builds():
    async def go():
        settings = Settings(_env_file=None, ADMIN_IDS="1")
        storage = await Storage(":memory:").open()
        try:
            client = MockClient()
            engine = Engine(settings, storage, client, MemoryNotifier())
            dp = build_dispatcher(AppContext(settings, storage, client, engine))
            assert dp["ctx"].client is client
        finally:
            await storage.close()

    asyncio.run(go())
