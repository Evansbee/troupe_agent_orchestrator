"""#108 QA soak review: a burst of live events used to schedule one full pane reload per event,
stacking overlapping in-flight client.call()s until they piled up past the connection's timeout.
Fast, Textual-free unit tests for the two small primitives that fix that."""
import asyncio

from troupe.tui.panes import AutoRetrier, ReloadCoalescer


def test_coalescer_runs_immediately_when_idle():
    calls = []

    async def fn():
        calls.append(1)

    asyncio.run(ReloadCoalescer(fn).trigger())
    assert calls == [1]


def test_coalescer_collapses_a_burst_into_one_trailing_call():
    calls = []
    release = asyncio.Event()

    async def fn():
        calls.append(1)
        if len(calls) == 1:
            await release.wait()  # hold the first call "in flight"

    async def scenario():
        c = ReloadCoalescer(fn)
        first = asyncio.ensure_future(c.trigger())
        await asyncio.sleep(0)  # let the first call actually start and start waiting
        # a burst of N triggers while busy must coalesce into at most one trailing call
        for _ in range(10):
            asyncio.ensure_future(c.trigger())
            await asyncio.sleep(0)
        release.set()
        await first
        await asyncio.sleep(ReloadCoalescer.DEBOUNCE + 0.05)  # let the trailing call run

    asyncio.run(scenario())
    assert calls == [1, 1]  # the in-flight call, plus exactly one trailing -- never N+1


def test_coalescer_lets_a_second_call_run_once_the_first_is_done():
    calls = []

    async def fn():
        calls.append(1)

    async def scenario():
        c = ReloadCoalescer(fn)
        await c.trigger()
        await c.trigger()

    asyncio.run(scenario())
    assert calls == [1, 1]  # not coalesced -- the first had already finished


def test_retrier_schedules_after_backoff_and_resets_on_reset():
    calls = []

    async def load():
        calls.append(1)

    async def scenario():
        r = AutoRetrier(load)
        r.schedule()
        assert calls == []  # not immediate
        await asyncio.sleep(1.2)  # AUTO_RETRY_MIN is 1.0s
        assert calls == [1]
        r.reset()
        r.schedule()  # backoff restarts from the minimum after a reset
        await asyncio.sleep(1.2)
        assert calls == [1, 1]

    asyncio.run(scenario())


def test_retrier_does_not_pile_up_multiple_pending_schedules():
    calls = []

    async def load():
        calls.append(1)

    async def scenario():
        r = AutoRetrier(load)
        r.schedule()
        r.schedule()  # a second schedule() while one is already pending must be a no-op
        r.schedule()
        await asyncio.sleep(1.2)
        assert calls == [1]  # not three

    asyncio.run(scenario())
