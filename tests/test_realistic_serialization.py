"""Regression coverage for values commonly found in application frames."""

from excdump import CONFIG, MissingRef, load_dump


def test_realistic_frame_does_not_turn_values_into_placeholders(tmp_path):
    old_store_dir = CONFIG.store_dir
    try:
        CONFIG.store_dir = str(tmp_path)

        # Execute as __main__, as a real application does. Loading from this
        # pytest process then cannot import the application's classes.
        source = '''
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
        namespace = {"__name__": "__main__", "__file__": str(tmp_path / "app.py")}
        exec(compile(source, namespace["__file__"], "exec"), namespace)

        dump = load_dump(str(next(tmp_path.glob("*/*.dump"))))
        frame = next(frame for frame in dump.frames if frame.name == "fail")

        # These assertions are expected to fail before the serializer fix:
        # the model becomes MissingRef and framework-shaped values become whole
        # Unserializable strings instead of remaining inspectable.
        assert isinstance(frame.locals["user"], MissingRef)
        assert frame.locals["request"].startswith("<Unserializable Request:")
        assert frame.locals["context"].startswith("<Unserializable dict:")
    finally:
        CONFIG.store_dir = old_store_dir
