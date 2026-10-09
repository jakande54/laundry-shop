"""
Vercel entrypoint. Vercel's Python runtime imports this module and looks
for a WSGI app object — it does NOT run app.py as a script, so the
`if __name__ == '__main__':` block in app.py never executes here. That
block is where init_db() runs locally, which is why a fresh Vercel deploy
can look "down": the tables were never created.

This file imports the Flask app from app.py, makes sure the schema exists,
and exposes `app` for Vercel.

The dashboards stay up to date by polling the /api/... endpoints every few
seconds (see app.py), so no WebSocket / Socket.IO support is needed here.
"""

import os
import sys

# Make sure app.py (one level up, in the project root) is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app, init_db  # noqa: E402

# Safe to run on every cold start: init_db() only uses IF NOT EXISTS /
# ON CONFLICT DO NOTHING, so it just confirms the schema is already there.
init_db()