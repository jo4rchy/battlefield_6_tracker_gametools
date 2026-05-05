"""BF6 Tracker — Gametools → TRN-shaped API.

Modules:
    main        core library: GametoolsClient, StatsStorage, delta builders
    converter   Gametools stats/profile -> TRN battlefield-tracker profile shape
    api         FastAPI app exposing /profile /matches /search /ping
"""

# ---------------------------------------------------------------------------
# Single source of truth for the project version.
#
# Bump THIS line and only this line when cutting a new release. Everything
# else (FastAPI app metadata, /ping + /status responses, Dockerfile LABEL,
# build.sh image tag, build.sh tarball filename, etc.) reads from here at
# import time, so there is nothing else to keep in sync.
#
# Format: "MAJOR.MINOR.PATCH" or "MAJOR.MINOR.PATCH.HOTFIX". Any string is
# accepted by FastAPI's `version=` and shows up verbatim in the OpenAPI
# spec, so keep it short and human-readable.
# ---------------------------------------------------------------------------
__version__ = "0.0.6"
