# excdump

Capture everything about an exception the moment it happens, then debug it later
on your own machine — with the frames, the locals, related globals, and the source files as they were.

A traceback tells you where a program failed. It does not store variables, and it's hard to understand `excdump` writes the exception to a file at the moment of failure, and
gives you an offline debugger to walk it later.

```python
from excdump import dump_exception

try:
    handle_checkout()
except Exception:
    trace_id = dump_exception()   # returns the id -- log it
    raise
```

```console
$ uv run excdump inspect <trace-id>
```

## Quick start

1. Add `excdump` to your project:

   ```console
   $ uv add excdump
   ```

2. Create a demo dump in the store you choose:

   ```console
   $ uv run excdump demo --store ./exception-dumps
   ```

3. Inspect the last exception in the store (or use `trace-id` to inspect specific exception):

   ```console
   $ uv run excdump inspect --store ./exception-dumps
   ```

## Install

Add `excdump` as a library dependency to a Python project with uv:

```console
$ uv add excdump
```

This makes the capture API available to your application:

```python
from excdump import dump_exception
```

Run the command-line tool with uv:

```console
$ uv run excdump demo
$ uv run excdump inspect
```

Requires Python 3.11+.


## Capturing

Two ways in. `dump_exception()` is called from an `except` block and returns the
trace id, which is what you log:

```python
except Exception:
    logger.error("checkout failed", extra={"trace_id": dump_exception()})
    raise
```

`@dump_on_exception` wraps a function and captures anything that escapes it,
then re-raises unchanged. It takes options, or none at all:

```python
from excdump import dump_on_exception

@dump_on_exception
def handle_checkout(cart):
    ...

@dump_on_exception(on_dump=report_to_otel, n_depth_up=2)
def calculate_tax(order):
    ...
```

The decorated function is the pivot the session opens on, with the decorator's
own frames left out, so `up` reaches its real caller.

Both capture the frames of the traceback plus a configurable number of caller
frames above it, every exception in a `raise ... from ...` chain, and the source
of each file involved.

## Inspecting

```console
$ uv run excdump list                 # what the store holds, grouped by failure
$ uv run excdump inspect              # the most recent dump
$ uv run excdump inspect <trace-id>   # a specific one; a unique prefix works
$ uv run excdump inspect <path-id>    # newest dump of one failure
$ uv run excdump gc                   # reclaim paths nothing hits any more
```

`inspect` opens a `pdb`-like session over the dump. Frames, locals and globals
are all there; `--plain` forces the readline prompt instead of the TUI.

Nothing about the session touches the process that crashed — it no longer
exists. That is the point: you get to look around at your leisure, on a
different machine, days later.

## How dumps are filed

A dump is filed under its **exception path** — the `(filename, lineno)` list of
its traceback, hashed. One bug hit a million times shares one path, so retention
applies per failure and a hot loop cannot push a rarer failure out of the store.

```
.exception_dumps/
  .gc                              when the last sweep ran
  <path_id>/
    path.json                      the (filename, lineno) list, for humans
    sources/<hash>.json.gz         full text of a captured file
    <trace_id>.dump                one capture
```

Nothing is ever read to decide where a dump goes, so many processes can write
into one store with no coordination between them.

Three things bound the size:

| | bound by | when |
|---|---|---|
| dumps within a path | count, `max_dumps_per_path` | every capture |
| whole paths | age, `max_path_age_days` | hourly during capture, and `gc` |
| abandoned temporaries | age, one hour | with the sweep above |

Paths need collecting because their ids churn: the id hashes line numbers, so a
deploy that shifts a line puts every traceback through that file under a new id
and leaves the old one unreachable. Age tells those apart from failures that
simply have not recurred yet — and needs no deploy hook to do it.

Removing a path removes everything it held. No rule here reads anything but file
names, which is why the sweep can run during capture without loading a thing.

## Source is stored, not re-read

Each captured file's full text is written into the path's `sources/` directory,
named by the hash of its contents. The inspector reads *that*, not the file on
disk.

This matters more than it sounds. Read the live file back and every dump written
before your last edit points its arrow at the wrong line — silently, and most
confusingly for the old dumps you most want to trust. Content addressing also
means a file edited between two dumps of one path simply lands in a second blob,
and two processes writing the same text write the same bytes to the same name.

## What gets stored for a value

Each captured value is stored the cheapest way that keeps it, decided per value:

- **plain pickle** if pickle can take it and the result will resolve wherever
  the dump is read;
- otherwise **dill**, which handles functions, classes and closures pickle
  refuses. Dill-backed values are kept in independently loadable payloads, so
  a framework object that dill can write but cannot reconstruct does not turn
  every function in the frame into a placeholder.
  Both of dill's encodings are tried and the smaller kept: neither wins in
  general, and the one that lost by 8× on a batch of functions from one module
  won by 3× on a single decorated one.
- **modules** are stored by name and re-imported on load, rather than pickled
  whole;
- otherwise the value's **shape**: a container keeps its elements and an object
  keeps its attributes, each stored by these same rules in turn. This is what a
  pydantic model defined in your entry script gets — pickle can only write a
  `__main__.User` reference that resolves to nothing in the inspector, and dill
  refuses the model class outright, so the fields themselves are kept instead:

  ```
  (exc-dbg) user
  <User snapshot: User(id=1, name='Grace')>
  (exc-dbg) user.name
  'Grace'
  (exc-dbg) context["request"].user.id
  3
  ```

  Attributes read normally, so inspecting a snapshot reads like inspecting the
  original. Expansion is bounded by `max_expand_depth` and `max_expand_items`,
  and cycles — the norm in framework object graphs — terminate;
- anything left becomes a capped `repr`, visible as a placeholder rather than a
  failed load.

A value is never stored as a reference that only resolves in the process that
wrote it. Such a reference reads back as `<Unavailable __main__.User>` at best,
and where the value is rebuilt *through* the missing class, unpickling raises
and the whole dump is lost.

A dump that cannot be fully reconstructed still opens. A class this machine
cannot import shows as `<Unavailable pkg.Thing>` and everything around it still
reads. A function from a module that is gone keeps its code and shows
`<Unavailable pkg.__dict__>` for its globals — readable, though calling it will
raise on the names that went with the module.


## Configuration

`configure()` validates as it sets, so a typo fails at startup rather than
mid-exception:

```python
from excdump import configure

configure(store_dir="/var/log/exception_dumps", max_dumps_per_path=500)
```

Every field also reads `EXCDUMP_<NAME>` from the environment, so a deployment
can tune capture without touching code. A malformed value is ignored rather than
raised — a bad environment variable must not stop an app from starting.

| option | default | meaning |
|---|---|---|
| `store_dir` | `./.exception_dumps` | root of the dump store |
| `max_dumps_per_path` | 200 | dumps kept per failure; 0 disables pruning |
| `max_path_age_days` | 14 | age at which a whole path is dropped; 0 keeps forever |
| `gc_interval_seconds` | 3600 | how often capture sweeps; 0 leaves it to `gc` |
| `n_depth_up` | 5 | caller frames captured above the handling frame |
| `n_depth_down` | 10 | traceback frames captured below it |
| `serializer` | `"dill"` | `"snapshot"`, `"dill"` or `"pickle"` |
| `source_radius` | 5 | lines kept either side of each captured line |
| `max_repr_chars` | 2000 | cap on a stored `repr` |
| `max_dill_bytes` | 65536 | backstop on one dill-serialized value |
| `max_expand_depth` | 3 | how deep a value is kept by shape; 0 disables |
| `max_expand_items` | 100 | elements kept per expanded container; 0 keeps all |
| `max_source_bytes` | 1000000 | cap on a captured file's stored text |
| `enabled` | `True` | master switch |
| `on_dump` | `None` | callback given each trace id |
| `verbose` | `False` | print the dump path to stderr |

## Capture never breaks the caller

Everything on the capture path is written to fail quietly. A store that cannot
be written, a value that cannot be serialized, a sweep that cannot run — none of
them raise, because all of it happens with an exception already in flight and
losing the original failure is always worse.

## Layout

| module | holds |
|---|---|
| `config.py` | `Config`, `configure()`, environment overrides |
| `paths.py` | trace and path ids, file-name conventions |
| `model.py` | `ExceptionDump`, `ExceptionRecord`, `FrameSnapshot` |
| `sources.py` | captured source and the per-path sidecar |
| `values.py` | which serializer each value gets |
| `capture.py` | `dump_exception`, `dump_on_exception` |
| `store.py` | on-disk layout, retention, collection, lookup |
| `loading.py` | reading a dump back, tolerantly |
| `session.py` | the offline debugging session |
| `cli.py` | commands and the readline inspector |
| `tui.py` | the full-screen inspector |
