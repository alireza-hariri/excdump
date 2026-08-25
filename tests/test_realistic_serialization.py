"""Regression coverage for values commonly found in application frames.

The dump has to be written by a *separate* process running the application as
``__main__``, which is what production looks like and what makes this bug
reproduce: an instance of a ``__main__`` class pickles only as a reference to
``__main__.User``, and this pytest process -- the inspector -- has a different
``__main__`` entirely. Running the application with ``exec`` in-process instead
does not reproduce it, because the class is then not reachable under any name
and pickle fails outright rather than writing a reference that dangles later.
"""

import subprocess
import sys

import pytest

from excdump import MissingRef, ValueSnapshot, load_dump


APP = '''
import asyncio
import contextvars
import re
import threading
from pydantic import BaseModel, TypeAdapter
from excdump import dump_exception

class User(BaseModel):
    id: int
    name: str

class Request:
    def __init__(self):
        self.user = User(id=3, name="Ada")
        self.lock = threading.RLock()
        self.callback = lambda: self.user.name

def fail():
    user = User(id=1, name="Grace")
    request = Request()
    context = {
        "user": user,
        "request": request,
        "adapter": TypeAdapter(User),
        "loop": asyncio.new_event_loop(),
        "context": contextvars.copy_context(),
        "pattern": re.compile(r"user-(\\d+)"),
    }
    raise RuntimeError("realistic failure")

try:
    fail()
except RuntimeError:
    dump_exception()
'''


@pytest.fixture
def frame(tmp_path):
    """The ``fail`` frame of a dump written by a real application process."""
    app = tmp_path / "app.py"
    app.write_text(APP)
    store = tmp_path / "dumps"
    subprocess.run(
        [sys.executable, str(app)],
        check=True,
        capture_output=True,
        env={"PATH": "/usr/bin", "EXCDUMP_DIR": str(store)},
    )
    dump = load_dump(str(next(store.glob("*/*.dump"))))
    return next(frame for frame in dump.frames if frame.name == "fail")


def test_main_defined_model_stays_inspectable(frame):
    """A pydantic model from the app's entry script must keep its field values.

    Neither serializer can carry it: pickle writes a ``__main__.User``
    reference that resolves to nothing here, and dill refuses the model class
    outright. What the frame held is still the two field values.
    """
    user = frame.locals["user"]
    assert not isinstance(user, MissingRef)
    assert isinstance(user, ValueSnapshot)
    assert user.type_name == "User"
    assert (user.id, user.name) == (1, "Grace")


def test_object_graph_is_kept_attribute_by_attribute(frame):
    """One unserializable attribute must not cost the whole object."""
    request = frame.locals["request"]
    assert isinstance(request, ValueSnapshot)
    assert request.type_name == "Request"
    # Reached through the nested model, which is a snapshot of its own.
    assert (request.user.id, request.user.name) == (3, "Ada")
    # A closure over an unstorable object is the one attribute that degrades,
    # and it does so alone rather than taking `user` down with it.
    assert isinstance(request.callback, str)
    assert request.callback.startswith("<Unserializable function:")


def test_container_keeps_its_shape_and_serializable_members(frame):
    """A dict holding framework objects stays a dict, not one repr string."""
    context = frame.locals["context"]
    assert isinstance(context, dict)
    assert set(context) == {
        "user", "request", "adapter", "loop", "context", "pattern"
    }
    # Plainly picklable members come back as themselves.
    assert context["pattern"].pattern == r"user-(\d+)"
    assert context["pattern"].match("user-42")
    # The app's own values stay inspectable inside the container too.
    assert (context["user"].id, context["user"].name) == (1, "Grace")
    assert context["request"].user.name == "Ada"


def test_framework_objects_are_expanded_rather_than_dropped(frame):
    """An object no serializer will take is still worth its attributes."""
    context = frame.locals["context"]
    loop = context["loop"]
    assert isinstance(loop, ValueSnapshot)
    assert "EventLoop" in loop.type_name
    assert loop.attrs  # the loop's state, not just its name


def test_nothing_in_the_frame_reads_back_as_a_missing_reference(frame):
    """A value pickled only as a ``__main__`` reference is never stored.

    Such a reference cannot resolve in the inspector, so it reaches this
    process as :class:`MissingRef` -- and when the value is rebuilt *through*
    the missing class, unpickling raises and the whole dump is lost. Anything
    that cannot travel is expanded or made a placeholder instead.
    """
    found = []
    for value in frame.locals.values():
        found.append(value)
        if isinstance(value, ValueSnapshot):
            found.extend(value.attrs.values())
        elif isinstance(value, dict):
            found.extend(value.values())
    assert found
    assert [v for v in found if isinstance(v, MissingRef)] == []
