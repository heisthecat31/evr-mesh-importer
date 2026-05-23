# Agent Instructions — EVR Raw Mesh Importer

This file contains structured instructions for AI agents (like Claude) working on this project in any mode (Plan, Agent, or Debug).

---

## 1. PROJECT OVERVIEW

**EVR Raw Mesh Importer** is a Blender add-on for importing/exporting raw GPU mesh binaries from **Echo VR (Echo Arena)**.

- **Repo:** https://github.com/Dualgame/evr-mesh-importer
- **License:** MIT
- **Blender:** 3.0+ (tested up to 5.x)
- **Python:** 3.10+ (bundled with Blender)
- **Current Version:** 1.2.4 (see `evr_mesh_importer/__init__.py`)

> **Vibe coded:** Entirely built with Claude AI reverse-engineering Echo VR binary formats from scratch. No official documentation or SDK was used.

---

## 2. FILE MAP

```
evr_mesh_importer/          # Core add-on package
├── __init__.py             # Main entry: registers Import/Export operators, version 1.2.4
├── decode.py               # GPU binary decoding (CIMR, CGML, heuristic) [LARGEST FILE]
├── primary.py              # Primary binary auto-discovery & path resolution
├── encode.py               # Mesh re-encoding to GPU binary format
└── textures.py             # PBR texture resolution & material creation

Root-level scripts:
├── build_zip.py            # Builds release/evr_mesh_importer_v*.zip
├── load_model_with_textures.py   # Standalone Blender texture loader script
├── apply_doug_textures_blender.py # Doug model texture applier
├── test.py                 # Texture hash extraction test
├── test.21.py              # Primary file analysis / diff diagnostic
├── test3.py                # Header comparison utility
├── investigate.md          # Format investigation notes (LARGE)
└── README.md               # User-facing documentation

.github/workflows/release.yml  # CI: builds release zip on tag push (v*.*.*)
```

---

## 3. CORE ARCHITECTURE

### 3.1 Data Flow (Import)

```
GPU Binary File (e.g., CGInstancedModelResource/<hash>)
         │
         ▼
   decode.py  ─── Primary file (optional, via primary.py)
         │
         ▼
   Submeshes: list of (verts, faces, uvs) tuples
         │
         ▼
   __init__.py  ─── Builds Blender mesh objects, applies UVs, shading, scale
         │
         ▼
   textures.py ─── (optional) Resolves & applies PBR materials
```

### 3.2 Data Flow (Export)

```
Blender Mesh Object
         │
         ▼
   encode.py ─── mesh_from_blender_object() (bmesh → triangulate → extract)
         │
         ▼
   Select encode path: heuristic_s16 | heuristic_s20 | heuristic_dual28 |
                        primary_described | cgml
         │
         ▼
   GPU binary (.bytes) + optional patched Primary binary
         │
         ▼
   Written to disk (__init__.py _write_gpu)
```

### 3.3 Decode Chain (Priority Order in decode.py)

| Priority | Path | Requires Primary | Description |
|----------|------|-----------------|-------------|
| 1 | `primary_described` | Yes | Uses Primary descriptor to interpret GPU stream layout |
| 2 | `crossref_ib` | Yes | Cross-references index buffer from Primary |
| 3 | `hero` | Yes | Hero-mode fallback |
| 4 | `cgml` | Yes | Map geometry decode path |
| 5 | `heuristic` | No | GPU-only heuristics (last resort) |

### 3.4 Encode Paths (in encode.py)

| Encode Mode | Vertex Stride | U16 Indices | Description |
|-------------|--------------|-------------|-------------|
| `heuristic_s16` | 16 bytes S0 + 28 bytes S1 | Yes | Standard stride-16 |
| `heuristic_s20` | 20 bytes S0 + 28 bytes S1 | Yes | White/alpha stride-20 |
| `heuristic_dual28` | 28 bytes S0 + 28 bytes S1 | Yes | Dual-28 padded |
| `primary_described` | Dynamic (16 or 20) | Yes | Full Primary-assisted encode |
| `cgml` | Per-submesh | Yes | Map geometry multi-submesh |

### 3.5 Supported Formats

| Format | Folder Name | Description | Submeshes |
|--------|-------------|-------------|-----------|
| CIMR | `CGInstancedModelResource` | Props, actors, cosmetics | Usually 1 (single mesh) |
| CGML | `CGMeshListResource` | Map base geometry | Multiple submeshes |

---

## 4. IMPORTANT CONSTRAINTS & GOTCHAS

### ⚠️ Vertex Limit
- **65535 vertices maximum** (uint16 index buffer). Meshes exceeding this must be decimated before export.
- Check: `if len(verts) > 65535: raise ValueError(...)` in encode.py

### ⚠️ GPU File Size
- When patching (primary_described_full_replace), the new GPU binary **must not exceed** the original GPU file size, or the RAD engine will crash.
- If smaller, zero-padding is applied to match original size.

### ⚠️ Primary Descriptor Structure
- Primary files contain 16-dword (64-byte) rendering descriptor blocks starting with `0x0B` as the block type/sentinel.
- Key fields in the descriptor block:
  - `[0]` = `0x0B` (block type)
  - `[4]` = stream0_size (bytes)
  - `[7]` = vertex_count (must match [8] and [11])
  - `[12]` = index_offset (byte offset in GPU file)
  - `[13]` = index_count (triangles × 3)
  - `[14]` = range_kind (`2` = linear/primary, other = LOD/shadow)

### ⚠️ CGML vs CIMR Difference
- CGML Primary files tend to be smaller and have **different descriptor block structure** vs CIMR
- CGML may have range_kind other than 2, which can cause issues with the current encode/primary patching

### ⚠️ LOD Stack Detection
- The importer auto-detects LOD stacks by comparing bounding box centers and diagonal sizes across submeshes
- When detected, only LOD0 is imported (configurable via `import_lod0_only`)

---

## 5. TEXTURE SYSTEM (textures.py)

### 5.1 Path Discovery
- Config files checked: `config.json` in `Tools/Settings/`
- PCVR extracted dir: `G:\pcvr-extracted` or from config
- Texture cache: typically in game install `bin/win10/Tools/Settings/texture_cache/`
- Metadata mapping folders: `23d48cecc462abe7` (Summer), `c2434c5a99e139ce` (Live PCVR)

### 5.2 Hash Name Variations
All hash names (model/texture/folder) can appear in multiple formats:
- Padded 16-char hex: `"c2434c5a99e139ce"`
- Stripped hex: `"c2434c5a99e139ce"` (same, but shorter if leading zeros)
- Unsigned decimal: `"14015384750234693070"`
- Signed decimal: `"-4364128495773922544"`

**Always use `get_all_name_variations()` when looking up files by hash!**

### 5.3 Material Slot Order (Echo PBR)
| Slot | Type | Colorspace |
|------|------|------------|
| 0 | Base Color (Albedo) | sRGB |
| 1 | Normal Map | Non-Color |
| 2 | Packed Material (R: Metallic, G: Roughness) | Non-Color |
| 3 | Emissive/Detail | sRGB |

### 5.4 Pixel-Based Texture Classifier
Classifies textures when binding info is unavailable:
- **normal_rg:** Very low blue, R/G centered ~0.5 (BC5/ATI2 format)
- **normal:** High blue (>0.55), R/G centered ~0.5 (standard 3-channel)
- **roughness:** Very low red (<0.05), high green+blue (>0.40) (ORM packed)
- **emissive:** Low avg brightness but has bright spots (max > 0.15)
- **metallic:** Grayscale, very low avg (<0.18)
- **albedo:** Everything else (default fallback)

### 5.5 Material Assignment Modes (from __init__.py)
- `auto` — Skin 0 for LOD/single imports, submesh order for multi-part
- `first` — Force Skin 0 on all meshes
- `submesh` — Sequential per submesh order
- `reverse` — Reverse submesh order

### 5.6 Texture Grouping Modes
- `sequential` — Groups of 4 textures in raw mapping order (matches verified Doug import)
- `binding` — Uses the binding table to determine which textures go in each slot

---

## 6. BUILD & RELEASE

```bash
python3 build_zip.py
# Output: release/evr_mesh_importer_v<version>.zip
```

**CI/CD:** GitHub Actions on tag push `v*.*.*` automatically builds and creates a release.

---

## 7. TESTING & DIAGNOSTICS

There are **no formal unit tests** yet (tests/ directory is empty).

### Available Diagnostic Tools

| Script | Purpose |
|--------|---------|
| `test.21.py` | Primary file analysis, GPU size comparison, block-by-block diff between original and new files |
| `test3.py` | Quick header comparison between working and broken model files |
| `test.py` | Texture hash extraction from hex data, copies matching PNGs from cache |

### How to run diff diagnostic:
```bash
python test.21.py <orig_gpu> <orig_primary> <new_gpu> <new_primary>
```

### Key patterns when debugging decode failures:
1. Check if Primary file exists and is found via `_find_primary_path()`
2. Compare Primary descriptor blocks (type `0x0B`) — vertex counts should match between [7], [8], [11]
3. Verify GPU file size hasn't changed unexpectedly
4. For CGML files, the Primary structure differs from CIMR — the `range_kind` field determines block type

---

## 8. COMMON TASKS & PATTERNS

### Adding a new decode path
1. Add the function in `decode.py`
2. Add it to the decode chain priority list
3. Update the import label in the status bar message

### Adding a new encode path
1. Implement the encoder function in `encode.py`
2. Add it as an `Encode Mode` enum item in `__init__.py`'s `EVR_OT_ExportMesh`
3. Handle the mode in `EVR_OT_ExportMesh.execute()`

### Adding new import/export options
1. Add `bpy.props` to the Operator class (e.g., `BoolProperty`, `EnumProperty`)
2. Use the property in `execute()` or `_import_single()`
3. The property automatically appears in Blender's UI panel

### Debugging texture application
1. Check that `pcvr_extracted_dir` and `texture_cache_dir` resolve correctly
2. Verify the materials mapping file exists under `c2434c5a99e139ce/<model_hash>`
3. Check that PNG texture files exist in texture_cache with matching hash name variations
4. Run `load_model_with_textures.py` in Blender's scripting workspace for isolated testing

---

## 9. CURRENT LIMITATIONS

1. **One broken file** (hash `0x5343dd53eb81f3b`): Two independent mesh regions that can't be disambiguated generically
2. **CGML encoding issues:** Re-encoding CGMeshListResource files may produce corrupted output
3. **65535 vertex hard limit** due to uint16 index buffer
4. **Windows-only paths:** Several path fallbacks are hardcoded with Windows drive letters
5. **No automated tests** — all testing done manually in Blender

---

## 10. AGENT MODE GUIDELINES

When working in **Agent mode** (with write access):

### Before modifying code:
1. Read `project_notes.md` and `agents.md` for context
2. Identify which module(s) need changes
3. Check for related constants, enums, or options that may need updating across files

### Do NOT:
- Hardcode Windows-specific paths in new code (use fallback patterns from `textures.py` instead)
- Remove existing encode/decode paths without understanding their purpose
- Exceed the 65535 vertex limit without adding proper validation

### When stuck:
- Read `investigate.md` — contains detailed format analysis notes
- Run `test.21.py` with the problem files to get a structured diff
- Check if the issue is specific to CIMR vs CGML format differences

---

## 11. GLOSSARY

| Term | Meaning |
|------|---------|
| **GPU Binary** | The raw mesh binary file from Echo VR containing vertex data, index buffers, and stream data |
| **Primary Binary** | A metadata/descriptor file paired with the GPU binary describing vertex layout |
| **CIMR** | CGInstancedModelResource — instanced props format |
| **CGML** | CGMeshListResource — map geometry format (multi-submesh) |
| **Stream-0** | First vertex stream (usually UVs, color data) |
| **Stream-1** | Second vertex stream (usually position, normal, tangent) |
| **Stride** | Bytes per vertex record in a stream |
| **LOD** | Level of Detail — multiple mesh versions at different resolutions |
| **ORM** | Occlusion/Roughness/Metallic — packed texture format |
| **BC5/ATI2** | Two-channel normal compression (R= X, G= Y, reconstruct Z) |
| **RAD Engine** | Ready At Dawn's proprietary game engine used by Echo VR |

# Python Agent Addendum — Idiomatic, Strongly-Typed Python 3.14

*Authored by [github.com/metis-sprock](https://github.com/metis-sprock).
Generic edition — drop into any Python project. Tighten per project in a
sibling `AGENTS.md`.*

**Required reading** for ANY agent writing or reviewing Python code in a
project that adopts this addendum. Read this BEFORE writing any Python
code, BEFORE reviewing any Python PR, and BEFORE touching any `.py` file
in the repo.

---

## The Missile Knows Where It Is

> *The missile knows where it is, because it knows where it isn't.*

Python is permissive. It will run almost anything. That is exactly why this
document is structured by negation as much as assertion: the agent needs to
know where the boundaries ARE NOT, because the language will not tell it.
The "Never Use", "Never", and "What Not to Do" clauses are load-bearing —
they are how an agent triangulates the correct path. An agent that reads
only what something IS will guess at its boundaries. An agent that also
reads what it IS NOT will not.

### This IS

- A binding ruleset for writing and reviewing **strongly-typed** Python
  3.14 code in any project that adopts it (typically by linking from the
  repo's `AGENTS.md`).
- A discipline aimed at AI-agent maintainability: types are the contract
  that lets the next agent reason without guessing.
- A code-review gate: the "Code Review Hard Stops" table is enforced.

### This is NOT

- A Python tutorial. Reading this does not teach you Python — it
  disciplines an agent that already knows Python.
- A description of "Pythonic" idioms in the loose, community sense. It is
  a pinned, opinionated subset chosen for safety and AI legibility.
- Optional for "scripts", "notebooks-turned-modules", or "one-off CLIs".
  Every `.py` file committed to an adopting repo is in scope.
- A permission slip for `Any`, `# type: ignore`, or duck typing. Dynamic
  typing is a tool of last resort here, not a default.
- Compatible with older Python. Code targeting <3.14 in new projects is
  out of scope; do not "support both" without explicit ask.

### You MUST

- Run the **preflight** (below) at the start of every session: verify
  `uv`, `ruff`, `pyright`, `pytest`, `pip-audit` are installed; if any is
  missing, STOP and ask the user to install it.
- Run the full local gate before declaring work done: `ruff format
  --check`, `ruff check`, `pyright` (strict), `pytest`, `pip-audit` — all
  clean, on the change.
- Type every parameter and return — including `-> None`. Strict pyright
  must pass.
- Validate untrusted input with `pydantic` at the trust boundary; pass
  validated `@dataclass(slots=True, frozen=True)` instances inward.
- Use `httpx`, `structlog`, `asyncio.TaskGroup`, `uv`, `ruff`, `pyright`.
- Re-raise from broad `except` blocks — failing closed beats silent
  recovery in any code that touches security, billing, or persistence.

### You must NEVER

- Use `Any` without an inline `# reason:` justifying it, or `# type:
  ignore` without a rule code AND inline reason.
- Use `Optional[X]`, `Union[A, B]`, `List[X]`, `Dict[K, V]`, `Tuple[...]`
  — write `X | None`, `A | B`, `list[X]`, `dict[K, V]`, `tuple[...]`.
- Add `from __future__ import annotations` in new files (PEP 649 already
  defers evaluation in 3.14).
- Catch bare `except:` or `except Exception:` without re-raising.
- Use `eval`, `exec`, `pickle.loads` on untrusted input. Ever.
- Use `requests`, `datetime.utcnow()`, `print` for diagnostics, `assert`
  for runtime checks, `os.system`, or `subprocess(..., shell=True)`.
- Install tools globally, activate venvs manually, or commit a
  `requirements.txt` / `setup.py` to a new project.
- Add a type checker, linter, or test framework unilaterally — they shape
  CI behavior; ask the user first.

### Common Mistakes (what goes wrong when agents guess)

- Treating Python's permissiveness as license: untyped helpers, `dict[str,
  Any]` everywhere, "I'll add types later". Result: the next agent inherits
  a codebase where the type checker is silent and bugs hide as `AttributeError`
  at runtime.
- Mocking internals instead of designing a `Protocol` boundary. Result:
  tests pass while production breaks; refactors cascade through mocks.
- `asyncio.create_task(...)` without retaining the reference. Result: GC
  silently cancels the task; you debug a race that "can't happen".
- Catching broad exceptions to "make it robust". Result: critical-path
  failures become silent — security gates open, transactions half-commit,
  audits lie.
- Reaching for a metaclass / `getattr` plumbing when a `Protocol` plus a
  dict would do. Result: code only the original author can read; type
  checker gives up.
- Skipping `pyright` because "tests pass". Tests verify behavior on the
  paths you wrote; types verify the paths you forgot.

---

## Python Version

**Baseline: CPython 3.14** (released 2025-10-07). Check `.python-version` and
`pyproject.toml` `requires-python` before writing code. Never target 3.12 or
earlier in new code. Never write code that only works on the free-threaded
build unless the package is explicitly marked `Free-threaded :: True`.

### Use These (3.13–3.14 features)

| Feature | Notes |
|---|---|
| PEP 649 deferred annotations | Default in 3.14; no more `from __future__ import annotations` in new files |
| `annotationlib` module | Introspect annotations as real objects, not strings |
| `int \| str` union syntax | `types.UnionType is typing.Union` since 3.14 — never `Union[...]` |
| `list[str]`, `dict[str, int]` | Builtin generics — never import `List`, `Dict` from `typing` |
| PEP 750 t-strings | `t"..."` returns `Template`; use for SQL, HTML, shell — anywhere f-strings invite injection |
| `typing.TypeIs` (3.13) | Narrowing user-defined type guards; replaces most `TypeGuard` uses |
| `typing.ReadOnly` on `TypedDict` | Mark immutable keys (3.13) |
| `warnings.deprecated` (3.13) | Decorator + type-checker integration |
| PEP 695 type params | `def f[T](x: T) -> T:` and `type Alias = ...` (3.12+) |
| `@override` decorator | From `typing`; mandatory on overriding methods (3.12+) |
| Free-threaded build (PEP 779) | Officially supported in 3.14 — opt-in only, requires C-ext audit |
| Subinterpreters (`concurrent.interpreters`) | Stdlib in 3.14 for true parallelism with isolation |
| `Self` type | From `typing`; prefer over `T = TypeVar("T", bound="Cls")` |

### Never Use (deprecated, removed, or anti-idiomatic)

- `from __future__ import annotations` — obsolete under PEP 649
- `typing.List`, `typing.Dict`, `typing.Tuple`, `typing.Set`, `typing.Type` — use builtins
- `typing.Optional[X]` — write `X | None`
- `typing.Union[A, B]` — write `A | B`
- `Any` without an inline `# reason:` comment justifying it
- `# type: ignore` without rule code AND inline reason: `# type: ignore[arg-type]  # reason: ...`
- `eval`, `exec`, `pickle.loads` on untrusted input — ever
- `os.system`, `shell=True` in `subprocess` — use argv lists
- `requests` — use `httpx` (sync + async, HTTP/2, typed)
- `datetime.utcnow()` — use `datetime.now(tz=UTC)`; naive datetimes are banned
- `print` for diagnostics — use `logging` / `structlog`
- `assert` for runtime validation — stripped under `-O`; raise explicitly
- `setup.py`, `setup.cfg`, `requirements.txt` — `pyproject.toml` only
- `poetry`, `pipenv`, `pip-tools`, `virtualenv`, `pyenv` for new projects — use `uv`
- `black`, `isort`, `flake8`, `pylint`, `autoflake` — use `ruff` (format + lint)
- `unittest.TestCase` — use `pytest` functions
- `mock.patch` of internal modules — pass dependencies as parameters

---

## Tooling (single Astral-aligned stack)

| Role | Tool | Notes |
|---|---|---|
| Interpreter, venv, deps, lockfile, runner | `uv` | Replaces pip, pip-tools, pyenv, virtualenv, poetry |
| Format + lint | `ruff` | Single tool; configured in `pyproject.toml` |
| Type checker (primary) | `pyright` (strict) | Stable, mature, 98% spec conformance |
| Type checker (secondary) | `ty` | Astral's checker — opt-in once 1.0 ships; fine as a fast pre-commit check today |
| Runtime validation | `pydantic` v2 | At trust boundaries only |
| Tests | `pytest` + `pytest-anyio` | `anyio` for async test backend portability |
| Task runner | `just` | Same as Go projects |
| Vuln scan | `uv pip audit` or `pip-audit` | CI gate |

Never install tools globally. Never `source .venv/bin/activate`. Always
`uv run <tool> <args>` — that resolves the environment automatically.

---

## Preflight: Verify Tooling BEFORE Writing Code

You MUST run a tooling preflight at the start of any Python work session in
a repo. If any tool is missing, **stop and ask the user to install it** —
do not proceed, do not silently downgrade to a weaker check, and do not
"just skip the type check this time". A change that lands without static
analysis is a change that lands blind.

```bash
# Run these first. Each must succeed.
command -v uv          || echo "MISSING: uv"
uv --version
uv run ruff --version       # format + lint
uv run pyright --version    # primary type checker (strict)
uv run pytest --version
uv run pip-audit --version  # vulnerability scan
# optional but encouraged in Astral-aligned repos:
uv run ty --version 2>/dev/null || true
```

If `uv` itself is missing, ask the user to install it:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

If a dev dependency is missing from `pyproject.toml`'s `[dependency-groups]
dev` array, ask the user before adding it. Do not add type checkers,
linters, or test frameworks unilaterally — they shape CI behavior.

### Run static analysis on EVERY change, before declaring done

Treat these as mandatory gates, not suggestions. Run them locally before
handing back to the user — CI failures from skipped local checks waste
review cycles:

```bash
uv run ruff format --check .   # formatting
uv run ruff check .            # lint (lint rules from pyproject.toml)
uv run pyright                 # strict type check
uv run pytest -x               # tests
uv run pip-audit               # known CVEs in dependencies
```

A green `pytest` with red `pyright` is not "done" — it is a partially-typed
change masquerading as a working one. Fix the types.

### When the user reports a bug

Before patching: reproduce with a failing test, then run `pyright` on the
affected module. A large fraction of "bugs" in Python codebases are type
errors that were silenced by `# type: ignore` or hidden by `Any`. Look there
first.

---

## Typing — The Hard Rules

This codebase is reviewed and extended by AI agents. Strong typing is what
keeps them from guessing. Everything below is non-negotiable.

- **Every function** has typed parameters and a typed return — including
  `-> None`. Pyright is configured `strict`; CI fails on any error.
- **No `Any`** in production code without an inline `# reason:` comment. If
  you need a "shape", reach for `TypedDict`, `Protocol`, or `object`.
- **Public APIs** must use `Protocol` (structural) over `ABC` (nominal).
  Define protocols at the CONSUMER, not the producer — same rule as Go
  interfaces.
- **Data classes:**
  - Internal data: `@dataclass(slots=True, frozen=True, kw_only=True)`.
  - External data (HTTP, queues, files, env, untrusted input): `pydantic.BaseModel`.
  - JSON-shaped dicts crossing process boundaries: `TypedDict`.
  - Never use plain `dict[str, Any]` to represent a known shape.
- **Generics:** use PEP 695 syntax (`def f[T](...)`) — never `TypeVar` at module scope unless you need `bound=` or `constraints`.
- **Narrow with `TypeIs`** for user-defined predicates. Use `match` for sum-type dispatch.
- **`Self`** from `typing` for fluent / builder return types.
- **`@override`** is mandatory on any method that overrides a base.
- **`Final`** on module-level constants. Constants are `UPPER_SNAKE`.
- **No bare `except:` and no `except Exception:` without re-raise.** Catch
  the narrowest class possible. If you must catch broadly (top of a worker
  loop), log with traceback and re-raise or exit.

### Pyright config (in `pyproject.toml`)

```toml
[tool.pyright]
pythonVersion = "3.14"
typeCheckingMode = "strict"
reportMissingTypeStubs = "error"
reportImplicitOverride = "error"
reportUnnecessaryTypeIgnoreComment = "error"
reportUntypedFunctionDecorator = "error"
reportUnknownParameterType = "error"
reportUnknownMemberType = "error"
reportUnknownVariableType = "error"
```

---

## Naming

| Entity | Rule | Example |
|---|---|---|
| Modules / packages | `lower_snake`, short, singular | `store`, `relay`, `audit` |
| Classes / types / `TypedDict` | `CapWords` | `ClientConfig`, `UserRow` |
| Functions / methods / vars | `lower_snake` | `parse_url`, `user_id` |
| Constants | `UPPER_SNAKE` + `Final` | `MAX_RETRIES: Final = 5` |
| Protocols (single-method) | `-er` / `-able` | `Signer`, `Loggable` |
| Protocols (multi-method) | noun | `UserStore`, `EventSink` |
| Errors | `<Name>Error` | `ValidationError`, `RateLimitError` |
| Type aliases | `CapWords` via PEP 695 | `type UserID = str` |
| Test files | `test_<module>.py` | `test_relay.py` |
| Private | leading `_` | `_compute_signature` |

**Never** name a variable `data`, `info`, `result`, `obj`, `tmp`, `foo`.
**Never** shadow stdlib names (`id`, `type`, `list`, `filter`, `input`).
**Never** abbreviate domain terms (`usr`, `req`, `cfg`); short loop names
(`i`, `k`, `v`) are fine inside ≤5-line scopes only.

---

## Project Layout (src/ layout, always)

```
src/<package>/         # Importable code; nothing above this in sys.path
tests/                 # Mirrors src/<package>/ layout
tests/integration/     # marked @pytest.mark.integration
testdata/              # Fixtures
scripts/               # One-shot helpers
pyproject.toml         # Single source of project config
uv.lock                # Committed
.python-version        # Matches requires-python lower bound
justfile               # Developer workflows
```

- Tests live in `tests/`, NOT next to source. (Different from Go.)
- Integration tests: `@pytest.mark.integration`, off by default.
- No `__init__.py` gymnastics — `src/` layout removes the need.
- Never put helpers in `conftest.py` that are imported by application code.

---

## Errors

```python
class RateLimitError(Exception):
    """Upstream rate limit hit; caller should back off."""

try:
    response = client.fetch(url)
except RateLimitError:
    raise
except Exception as exc:
    raise FetchError(f"fetch failed for {url!r}") from exc
```

- Always `raise X from exc` when wrapping — preserves the cause chain.
- Error messages: lowercase, no trailing punctuation, no f-string secrets.
- Never swallow exceptions in critical paths (security, billing,
  persistence, audit). Re-raise or terminate.
- `sys.exit` / `os._exit` only in `__main__` / CLI entrypoints.
- Use `ExceptionGroup` and `except*` for concurrent task fan-out (3.11+).

---

## Concurrency

- Default to `asyncio` for I/O. `anyio` if the library must support trio.
- `asyncio.TaskGroup` over `asyncio.gather` — structured concurrency, proper
  cancellation, `ExceptionGroup` semantics.
- Never `asyncio.create_task(...)` without holding the reference — GC will
  cancel it silently.
- No `time.sleep` in async code — `await asyncio.sleep`.
- No `threading` for CPU work on the default GIL build. Use
  `concurrent.interpreters` (3.14) or a subprocess pool.
- Free-threaded build: opt-in per service only; every C extension must
  declare `Py_mod_gil = Py_MOD_GIL_NOT_USED` or be excluded.
- Always pass `timeout=` to network calls. No unbounded waits.

---

## Logging

`structlog` over stdlib `logging` for application code; stdlib for libraries.

```python
import structlog

log = structlog.get_logger(__name__)
log.info("user.created", user_id=u.id, latency_ms=elapsed.total_seconds() * 1000)
```

- Structured key-value only. Never f-string the message.
- Never log secrets, tokens, raw request bodies, or PII. If the project
  has a separate audit channel, route audit-relevant events there, not to
  the application log.
- Use event names like `noun.verb` (`user.created`, `request.denied`).

---

## Testing

### TDD: red, green, refactor. Always.

```python
import pytest

pytestmark = pytest.mark.anyio  # whole-file async marker

@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"

@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("a@b.co", True),
        ("nope",   False),
    ],
)
def test_is_email(raw: str, want: bool) -> None:
    assert is_email(raw) is want
```

- Parametrize over branchy logic — no copy-pasted near-duplicate tests.
- `pytest-anyio` for async tests with an explicit `anyio_backend` fixture.
- No `time.sleep` in tests — use `anyio.fail_after`, conditions, or fake clocks.
- No network in unit tests. `httpx.MockTransport` for HTTP boundaries.
- Mock at the protocol boundary you defined — never monkey-patch internals.
- Coverage on changed files must not decrease.
- Fuzz parsers and public entrypoints with `hypothesis`.

---

## `pyproject.toml` skeleton

```toml
[project]
name = "your-project"
requires-python = ">=3.14"
dynamic = ["version"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependency-groups]
dev = ["pytest", "pytest-anyio", "hypothesis", "pyright", "ruff"]

[tool.ruff]
target-version = "py314"
line-length = 100

[tool.ruff.lint]
select = ["ALL"]
ignore = ["D203", "D213", "COM812"]  # only if conflicting with formatter

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["S101", "PLR2004"]  # asserts and magic numbers ok in tests

[tool.pytest.ini_options]
addopts = "-ra --strict-markers --strict-config"
markers = ["integration: requires external services"]
```

---

## Justfile

```just
default: fmt lint type test
fmt:       uv run ruff format .
lint:      uv run ruff check --fix .
type:      uv run pyright
test:      uv run pytest -x
test-all:  uv run pytest --run-integration
bench:     uv run pytest tests/bench --benchmark-only
audit:     uv run pip-audit
lock:      uv lock
upgrade:   uv lock --upgrade
ci:        fmt lint type test audit
```

---

## Code Review Hard Stops

| # | Rule |
|---|---|
| 1 | `ruff format --check .` zero output |
| 2 | `ruff check .` zero output |
| 3 | `pyright` zero errors in strict mode |
| 4 | `pytest` green; coverage on changed files does not drop |
| 5 | No `Any` without inline `# reason:` |
| 6 | No `# type: ignore` without rule code AND inline reason |
| 7 | No `Optional[X]` / `Union[...]` / `List[...]` / `Dict[...]` — use `X \| None`, `\|`, `list[...]`, `dict[...]` |
| 8 | No `from __future__ import annotations` in new files |
| 9 | No `print`, no bare `assert` for runtime checks, no `eval`/`exec` |
| 10 | No `requests` — use `httpx`. No `datetime.utcnow()` — use `now(tz=UTC)` |
| 11 | No `asyncio.create_task` without retained reference; prefer `TaskGroup` |
| 12 | Every public function has a docstring AND complete type annotations |
| 13 | `uv lock` produces no diff |
| 14 | `pip-audit` clean |
| 15 | No secrets, tokens, or PII in logs |
| 16 | Preflight passed: `uv`, `ruff`, `pyright`, `pytest`, `pip-audit` all present and run on the change |

---

## What Not to Do

- No `__init__.py` that re-exports half the package "for convenience" — explicit imports.
- No metaclasses when `__init_subclass__`, decorators, or `dataclass_transform` will do.
- No `getattr`/`setattr`/`hasattr` for dynamic dispatch — use a `Protocol` and a dict.
- No global mutable state. Singletons go through dependency injection.
- No `time.sleep` in production code paths — back-off via async timer.
- No mixing of sync and async I/O in the same request path.
- No catching `BaseException` outside `__main__`.
- No bare `pass` in `except:` — log and re-raise, or document why.
- No `# noqa` without a rule code AND a reason.
- No "temporary" `Any` — file an issue or fix it; "temporary" becomes permanent.
- Security-, billing-, and persistence-critical paths **fail closed**: any
  exception denies / aborts. Never write a `try / except / return True`
  path in those layers.

---

## Sources

Built from the Python 3.14 release notes and current (2026) ecosystem
guidance:
- Python 3.14 What's New — <https://docs.python.org/3/whatsnew/3.14.html>
- PEP 649 (deferred annotations) — <https://peps.python.org/pep-0649/>
- PEP 750 (t-strings) — <https://peps.python.org/pep-0750/>
- PEP 779 (free-threaded official support) — <https://docs.python.org/3/howto/free-threading-python.html>
- Astral toolchain (uv, ruff, ty) — <https://astral.sh/blog/ty>, <https://docs.astral.sh/ty/>
- Pyright — <https://github.com/microsoft/pyright>
- Pydantic v2 — <https://docs.pydantic.dev/>
