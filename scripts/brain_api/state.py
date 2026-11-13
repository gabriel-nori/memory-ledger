"""Process-wide facts the routes report, set once by `serve.py` at start."""

from __future__ import annotations

import os
import time

# Writes (POST /api/*) are enabled unless started with --no-write. The write API
# is unauthenticated, so bind to 127.0.0.1 or pass --no-write when exposing it.
WRITABLE = True

# Host names a write may be addressed to or come from (the Host and Origin headers).
# serve.py adds the --host it was started with, for a deliberate non-local bind.
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
BOUND_HOST = ""


def extra_hosts() -> frozenset:
    return frozenset({BOUND_HOST.lower()}) if BOUND_HOST and BOUND_HOST not in ("0.0.0.0", "::") else frozenset()


# Identifies THIS process. The page polls it and reloads itself when it changes,
# so a restart carries the new page to an already-open tab.
BOOT = f"{os.getpid()}-{int(time.time())}"

# Why the UI could not be (re)built, when it could not — shown on the fallback
# page rather than a bare 404. Set by serve.ensure_ui().
UI_PROBLEM = ""

# Whether the app's lifespan starts the Slack/Jira trigger watchers
# (brain_api/trigger_runtime.py). Off unless serve.py turns it on, so a server
# built by a test never reaches out to Slack.
TRIGGERS = False

# Whether the lifespan runs the two-way neo4j sync loop (brain_api/sync_runtime.py).
# Off unless serve.py turns it on, for the same reason.
SYNC = False
