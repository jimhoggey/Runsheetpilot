"""Tests for the native (pywebview) desktop window.

The app used to show a tkinter status window and open the UI in the
system browser. It now opens the UI in a native window — same
main-thread-ownership architecture (waitress serves from a daemon
thread), with the old Tk-window+browser combination kept as the fallback
for machines where pywebview can't start (e.g. a Windows 10 install
without the WebView2 runtime).
"""
import sys
import types

import pytest

from propresenterrunsheet import server


def _fake_webview(record, start_raises=None):
    m = types.SimpleNamespace()
    def create_window(title, url, **kw):
        record["title"], record["url"], record["kw"] = title, url, kw
        return object()
    def start():
        record["started"] = True
        if start_raises:
            raise start_raises
    m.create_window, m.start = create_window, start
    return m


def test_native_window_runs_and_reports_shown():
    record = {}
    assert server._run_native_window(5757, webview_module=_fake_webview(record))
    assert record["title"] == server.APP_NAME
    # 127.0.0.1, not localhost — proxies on managed Windows machines
    # intercept "localhost" (the CI smoke test learned this the hard way).
    assert record["url"] == "http://127.0.0.1:5757"
    assert record["started"] is True


def test_native_window_failure_reports_false_not_crash():
    """A machine without a usable webview backend (missing WebView2
    runtime, broken pythonnet bundling) must fall back, not die."""
    record = {}
    fake = _fake_webview(record, start_raises=RuntimeError("no backend"))
    assert server._run_native_window(5757, webview_module=fake) is False


def test_window_flag_forces_the_window_from_source(monkeypatch):
    """`--window` lets a developer see the native window without freezing
    a bundle — from source on Mac/Linux the default is still headless."""
    monkeypatch.setattr(sys, "argv", ["propresenter_app.py", "--window"])
    assert server._should_show_status_window() is True


def test_headless_beats_window_flag(monkeypatch):
    """CI passes --headless; nothing may override it — the runners have
    no interactive desktop and a window attempt would hang the smoke."""
    monkeypatch.setattr(sys, "argv", ["x", "--window", "--headless"])
    assert server._should_show_status_window() is False
