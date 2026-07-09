#!/usr/bin/env bash
# Compile selected backend/app/* trees into Cython extensions (.so).
#
# Runs in the ``backend-compile`` Docker stage. Source tree lives at
# /build/backend (see Dockerfile). For each .py target this script:
#   1. Calls cythonize() to produce <name>.c next to it.
#   2. Builds the .c into <name>.cpython-<py>-<plat>.so in place.
#   3. Removes <name>.py and <name>.c so only .so reaches the next stage.
#
# Cythonize derives the module name from the path layout
# (``app/foo/bar.py`` -> ``app.foo.bar``). At runtime Python locates the
# .so via the same path layout — the init function PyInit_<basename> is
# package-agnostic, so this .so is importable as both ``app.foo.bar``
# (used by uvicorn/celery in the final image, cwd=/app) and as
# ``backend.app.foo.bar`` (used by the test suite, cwd=/build).
#
# Pragmatic scope (matches plan):
#   compile:  app/collectors, app/services, app/providers,
#             app/core, app/api, app/routers, app/utils
#   exclude:  __init__.py, main.py, conftest.py, db/, schemas/,
#             templates/, tests/ subdirs
#
# Any failure aborts the build; the .py is only removed AFTER a
# successful .so is verified to exist on disk.

set -euo pipefail

ROOT="${BUILD_ROOT:-/build/backend}"
APP_DIR="${ROOT}/app"

if [[ ! -d "${APP_DIR}" ]]; then
    echo "ERROR: app dir not found at ${APP_DIR}" >&2
    exit 2
fi

cd "${ROOT}"

# cythonize infers the module name from the directory layout. If
# ``${ROOT}/__init__.py`` exists, it considers the cwd to be a package
# (here: ``backend``), and module names come out as ``backend.app.foo``
# instead of ``app.foo``. ``build_ext --inplace`` then tries to write
# the .so to ``backend/app/foo/...`` relative to cwd, which resolves
# to ``${ROOT}/backend/...`` — a directory that does not exist.
#
# We temporarily move __init__.py out of the way for the duration of
# the compile. The trap restores it on any exit (success or failure)
# so the test stage still sees the package.
INIT_HIDDEN=""
if [[ -f "${ROOT}/__init__.py" ]]; then
    mv "${ROOT}/__init__.py" "${ROOT}/.__init__.py.compile-stash"
    INIT_HIDDEN=1
    trap '[[ -n "${INIT_HIDDEN}" && -f "${ROOT}/.__init__.py.compile-stash" ]] && mv "${ROOT}/.__init__.py.compile-stash" "${ROOT}/__init__.py" || true' EXIT
fi

INCLUDE_DIRS=(
    "app/collectors"
    "app/services"
    "app/providers"
    "app/core"
    "app/api"
    "app/routers"
    "app/utils"
)

EXCLUDE_BASENAMES=(
    "__init__.py"
    "main.py"
    "conftest.py"
)
EXCLUDE_PREFIXES=(
    "app/db/"
    "app/schemas/"
    "app/templates/"
    "app/collectors/tests/"
    "app/collectors/normalize/tests/"
)

is_excluded() {
    local rel="$1"
    local base
    base="$(basename "${rel}")"
    for ex in "${EXCLUDE_BASENAMES[@]}"; do
        [[ "${base}" == "${ex}" ]] && return 0
    done
    for ex in "${EXCLUDE_PREFIXES[@]}"; do
        [[ "${rel}" == ${ex}* ]] && return 0
    done
    return 1
}

# Collect targets relative to ${ROOT}.
TARGETS=()
for sub in "${INCLUDE_DIRS[@]}"; do
    if [[ ! -d "${ROOT}/${sub}" ]]; then
        echo "WARN: include dir ${sub} not present; skipping" >&2
        continue
    fi
    while IFS= read -r -d '' f; do
        rel="${f#./}"
        if ! is_excluded "${rel}"; then
            TARGETS+=("${rel}")
        fi
    done < <(cd "${ROOT}" && find "${sub}" -type f -name "*.py" -print0)
done

COUNT="${#TARGETS[@]}"
if [[ "${COUNT}" -eq 0 ]]; then
    echo "ERROR: no .py targets selected for compilation" >&2
    exit 3
fi

echo "cython-build: compiling ${COUNT} module(s)…"

NUM_PROC="$(nproc 2>/dev/null || echo 2)"

# Write target list to a file to avoid argv length limits and quoting
# issues passing hundreds of paths through bash -> python.
TARGETS_FILE="$(mktemp)"
printf '%s\n' "${TARGETS[@]}" > "${TARGETS_FILE}"

python <<PY
import sys
from pathlib import Path
from Cython.Build import cythonize
from setuptools import Extension, setup

with open("${TARGETS_FILE}") as fh:
    targets = [line.strip() for line in fh if line.strip()]

# Use explicit Extension(name=...) so module names come from the path
# regardless of which subdirs happen to have __init__.py. Otherwise
# cythonize() walks parents looking for __init__.py to decide the
# package name, and any missing __init__.py turns 'app/services/foo.py'
# into bare 'foo' or 'threat_intel.foo' — breaking the --inplace copy.
exts = []
for path in targets:
    mod_name = path[:-3].replace("/", ".")  # 'app/foo/bar.py' -> 'app.foo.bar'
    exts.append(Extension(name=mod_name, sources=[path]))

sys.argv = ["setup.py", "build_ext", "--inplace", "-j", "${NUM_PROC}"]
setup(
    name="centralops_compiled",
    ext_modules=cythonize(
        exts,
        language_level="3str",
        nthreads=${NUM_PROC},
        compiler_directives={
            "embedsignature": False,
            "emit_code_comments": False,
            "always_allow_keywords": True,
            # CRITICAL for FastAPI routers: Cython 3 defaults
            # ``annotation_typing=True``, which turns ``var: list = Query(...)``
            # into a runtime type check that rejects the Query object. We
            # need annotations to remain pure metadata (the FastAPI/Pydantic
            # behavior) — disable Cython's interpretation entirely.
            "annotation_typing": False,
        },
    ),
)
PY

rm -f "${TARGETS_FILE}"

# Verify .so produced for every target.
MISSING=()
for rel in "${TARGETS[@]}"; do
    base_no_ext="${rel%.py}"
    if ! compgen -G "${base_no_ext}.*.so" > /dev/null; then
        MISSING+=("${rel}")
    fi
done

if [[ "${#MISSING[@]}" -gt 0 ]]; then
    echo "ERROR: no .so produced for ${#MISSING[@]} module(s):" >&2
    printf '  - %s\n' "${MISSING[@]}" >&2
    exit 4
fi

# Strip originals: .py (source) and .c (Cython intermediate).
for rel in "${TARGETS[@]}"; do
    rm -f "${rel}"
    rm -f "${rel%.py}.c"
done

# Also strip the build/ tree cythonize creates for intermediate objects.
rm -rf build/

echo "cython-build: compiled ${COUNT} module(s); .py and .c removed."
