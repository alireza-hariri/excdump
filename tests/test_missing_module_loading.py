"""Loading a dump whose values point at a module the inspector does not have.

The reason this is not exotic: dill stores a function whose ``__globals__`` is
a module's ``__dict__`` as a *reference* to that dict. Any module-level lambda
qualifies, because a lambda cannot be located by name and so is stored by
value. Redeploy, rename the module, inspect on another machine -- the reference
no longer resolves, and what it resolves to instead has to be a dict, since
rebuilding a function requires one.
"""

import shutil
import subprocess
import sys

import pytest

from excdump import MissingModuleDict, MissingRef, load_dump


HELPER = '''
CONST = 7

# Stored by value: a lambda has no locatable name, so dill writes its code
# plus a reference to this module's __dict__ as its globals.
scale = lambda x: x * CONST
'''

APP = '''
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helper import scale
from excdump import dump_exception

def fail():
    fn = scale
    raise RuntimeError("gone module")

try:
    fail()
except RuntimeError:
    dump_exception()
'''


@pytest.fixture
def dump_without_its_helper(tmp_path):
    """Dump a value from ``helper``, then take ``helper`` away again.

    Returns a loader rather than a dump, so a test can assert on the loading
    itself: the bug being covered lost the *whole* file, not one value.
    """

    def build(serializer):
        app_dir = tmp_path / serializer
        app_dir.mkdir()
        (app_dir / "helper.py").write_text(HELPER)
        (app_dir / "app.py").write_text(APP)
        store = app_dir / "dumps"
        subprocess.run(
            [sys.executable, str(app_dir / "app.py")],
            check=True,
            capture_output=True,
            env={
                "PATH": "/usr/bin",
                "EXCDUMP_DIR": str(store),
                "EXCDUMP_SERIALIZER": serializer,
            },
        )
        # The redeploy: the module the dump refers to is no longer importable.
        (app_dir / "helper.py").unlink()
        shutil.rmtree(app_dir / "__pycache__", ignore_errors=True)
        return str(next(store.glob("*/*.dump")))

    return build


def test_dill_mode_dump_still_loads_without_the_module(dump_without_its_helper):
    """The whole dump used to be lost to this, in every serializer mode.

    A dill-mode dump is one stream, so the reference resolving to a class --
    which ``FunctionType`` rejects for its globals -- raised part-way through
    unpickling and no frame of the dump could be read at all.
    """
    dump = load_dump(dump_without_its_helper("dill"))
    frame = next(frame for frame in dump.frames if frame.name == "fail")
    scale = frame.locals["fn"]
    assert callable(scale)
    assert scale.__name__ == "<lambda>"
    # The globals stand in as a dict, which is what made rebuilding possible.
    assert isinstance(scale.__globals__, MissingModuleDict)
    with pytest.raises(NameError):
        scale(2)  # `CONST` genuinely is not here; only that part is lost


def test_auto_mode_keeps_the_function_rather_than_a_missing_reference(
    dump_without_its_helper,
):
    """The isolated payload must be read as tolerantly as the outer stream.

    Auto mode never lost the dump -- each value travels in its own payload --
    but loading a payload with plain dill made one absent name cost the entire
    value, which then read back as :class:`MissingRef`.
    """
    dump = load_dump(dump_without_its_helper("auto"))
    frame = next(frame for frame in dump.frames if frame.name == "fail")
    scale = frame.locals["fn"]
    assert not isinstance(scale, MissingRef)
    assert callable(scale)
    assert scale.__name__ == "<lambda>"
