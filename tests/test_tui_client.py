"""#108: TuiClient.connect() must accept API responses at least as large as the server allows
(api.py's MAX_LINE), not asyncio's 64 KiB StreamReader default -- a realistically sized project's
`tasks`/`messages`/etc. response is comfortably over 64 KiB."""
import asyncio
import json

from tui_fixture import FixtureServer

from troupe.api import MAX_LINE
from troupe.tui.client import TuiClient

BIG_TASKS = [
    dict(id=i, status="ready", title="x" * 2000, assignee="builder-1", priority=1,
         flags=dict(human_request=False, checks_failed=False, arch_review=False, territory_conflict=[]))
    for i in range(200)
]


def test_client_accepts_a_response_over_the_default_64kib_limit(project):
    cfg, _store = project
    payload_size = len(json.dumps(dict(items=BIG_TASKS)))
    assert 64 * 1024 < payload_size < MAX_LINE  # this test is only meaningful in that window

    async def scenario():
        server = FixtureServer(cfg.root, tasks=BIG_TASKS)
        await server.start()
        try:
            client = TuiClient(cfg.root)
            await client.connect()
            result = await client.call("tasks", status=["ready"])
            assert len(result["items"]) == len(BIG_TASKS)
        finally:
            await client.close()
            await server.stop()

    asyncio.run(scenario())
