"""Monitor module for Leap.

Intentionally does NOT eagerly import :mod:`leap.monitor.app` (which
pulls in the heavy PyQt5 GUI stack).  Importing the package — e.g. for
``leap.monitor.navigation`` from the resume picker — must stay cheap and
must not require the monitor-only GUI dependencies.  Consumers that need
the GUI import it explicitly: ``from leap.monitor.app import main`` (see
``scripts/leap-monitor.py`` and the ``leap-monitor`` entry point).
"""
