"""Which frames a dump captures above the anchor.

Anchoring below the handling frame is how the inspector's cursor lands on the
code that actually failed. That only works if the frames above the anchor are
the ones that called it -- and the live stack is not that chain once a task is
involved, which is most of an async application.
"""

import asyncio
import glob
import sys

import pytest

from excdump import configure, dump_exception, dump_on_exception, load_dump


@pytest.fixture
def dumps(tmp_path):
    """Capture into ``tmp_path`` and read back the frame names in order."""
    old = configure().store_dir
    configure(store_dir=str(tmp_path))
    try:
        def names():
            dump = load_dump(glob.glob(str(tmp_path / "*" / "*.dump"))[0])
            return [frame.name for frame in dump.frames], dump.target_frame_index

        yield names
    finally:
        configure(store_dir=old)


def _raising_frame():
    """The innermost frame of the exception being handled."""
    tb = sys.exc_info()[2]
    while tb.tb_next is not None:
        tb = tb.tb_next
    return tb.tb_frame


def test_callers_come_from_the_traceback_across_a_task_boundary(dumps):
    """A task's outermost coroutine frame has no ``f_back``.

    Walking the live stack from an anchor below one therefore stops dead --
    or wanders into the event loop -- losing callers the traceback names
    perfectly well. Here that is ``outer``, which holds the request.
    """

    async def inner():
        raise RuntimeError("boom")

    async def middle():
        # The boundary: a task, not a plain await, so the live stack breaks.
        await asyncio.create_task(inner())

    async def outer():
        request = {"id": 7}
        try:
            await middle()
        except RuntimeError:
            return dump_exception(
                n_depth_up=5, n_depth_down=0, _anchor_frame=_raising_frame()
            ), request

    asyncio.run(outer())
    names, cursor = dumps()
    assert names[-1] == "inner"
    assert cursor == len(names) - 1  # the cursor stays on the raising frame
    # The callers the live stack could not reach.
    assert "outer" in names and "middle" in names


def test_anchoring_at_the_raiser_keeps_the_frame_holding_the_request(tmp_path):
    """The point of the fix: the cursor *and* the context, not one or the other.

    The values wanted first when a request fails -- the request, the
    conversation, whatever the outer frame was holding -- live in a frame the
    live-stack walk could not reach from the raiser.
    """
    old = configure().store_dir
    configure(store_dir=str(tmp_path))
    try:

        async def inner():
            raise RuntimeError("boom")

        async def middle():
            await asyncio.create_task(inner())

        async def outer():
            request = {"id": 7}
            try:
                await middle()
            except RuntimeError:
                dump_exception(
                    n_depth_up=5, n_depth_down=0, _anchor_frame=_raising_frame()
                )
            return request

        asyncio.run(outer())
        dump = load_dump(glob.glob(str(tmp_path / "*" / "*.dump"))[0])
        frame = next(f for f in dump.frames if f.name == "outer")
        assert frame.locals["request"] == {"id": 7}
    finally:
        configure(store_dir=old)


def test_handler_anchored_capture_still_walks_the_live_stack(dumps):
    """With the default anchor the traceback has nothing above it.

    The handling frame is the traceback's outermost entry, so its callers can
    only come from the live stack -- unchanged behaviour, and the reason the
    walk cannot simply be deleted.
    """

    def handler():
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            dump_exception(n_depth_up=3, n_depth_down=0)

    def middle():
        handler()

    def outer():
        middle()

    outer()
    names, cursor = dumps()
    assert names[-1] == "handler"
    assert names[cursor] == "handler"
    assert names[-3:-1] == ["outer", "middle"]


def test_decorator_still_hides_its_wrapper_frame(dumps):
    """The wrapper *is* the traceback's outermost frame above the anchor.

    So a traceback-based caller chain must not be used when a caller has said
    where the chain resumes; otherwise the frame the decorator exists to hide
    reappears in every dump it writes.
    """

    def inner():
        raise RuntimeError("boom")

    @dump_on_exception(n_depth_up=3, n_depth_down=2)
    def decorated():
        inner()

    def middle():
        decorated()

    def outer():
        middle()

    with pytest.raises(RuntimeError):
        outer()
    names, cursor = dumps()
    assert "wrapper" not in names
    assert names[-2:] == ["decorated", "inner"]
    assert names[cursor] == "decorated"
    assert "middle" in names and "outer" in names
