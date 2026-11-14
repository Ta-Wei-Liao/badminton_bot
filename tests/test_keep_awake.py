import logging
from types import SimpleNamespace

import pytest

from badminton_bot.utils import keep_awake as ka


class FakeProc:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


def make_fake_windll():
    calls = []
    kernel32 = SimpleNamespace(SetThreadExecutionState=lambda flags: calls.append(flags))
    return SimpleNamespace(kernel32=kernel32), calls


def patch_mac(monkeypatch):
    procs, argv = [], []

    def fake_popen(args, *a, **kw):
        argv.append(args)
        p = FakeProc()
        procs.append(p)
        return p

    monkeypatch.setattr(ka.sys, "platform", "darwin")
    monkeypatch.setattr(ka.subprocess, "Popen", fake_popen)
    return procs, argv


def test_mac_starts_and_stops_caffeinate(monkeypatch):
    procs, argv = patch_mac(monkeypatch)
    with ka.keep_awake():
        assert len(procs) == 1 and not procs[0].terminated
    assert argv[0][:3] == ["caffeinate", "-i", "-w"]
    assert argv[0][3].isdigit()
    assert procs[0].terminated


def test_windows_sets_and_restores_execution_state(monkeypatch):
    windll, calls = make_fake_windll()
    monkeypatch.setattr(ka.sys, "platform", "win32")
    monkeypatch.setattr(ka, "_get_windll", lambda: windll)
    with ka.keep_awake():
        assert calls == [0x80000001]
    assert calls == [0x80000001, 0x80000000]


def test_releases_even_when_the_body_raises(monkeypatch):
    procs, _ = patch_mac(monkeypatch)
    with pytest.raises(RuntimeError, match="boom"):
        with ka.keep_awake():
            raise RuntimeError("boom")
    assert procs[0].terminated

    windll, calls = make_fake_windll()
    monkeypatch.setattr(ka.sys, "platform", "win32")
    monkeypatch.setattr(ka, "_get_windll", lambda: windll)
    with pytest.raises(RuntimeError, match="boom"):
        with ka.keep_awake():
            raise RuntimeError("boom")
    assert calls == [0x80000001, 0x80000000]


def test_failure_to_start_only_warns(monkeypatch, caplog):
    def broken_popen(*a, **kw):
        raise FileNotFoundError("caffeinate")

    monkeypatch.setattr(ka.sys, "platform", "darwin")
    monkeypatch.setattr(ka.subprocess, "Popen", broken_popen)
    with caplog.at_level(logging.WARNING):
        with ka.keep_awake():
            pass
    assert "無法防止電腦睡眠" in caplog.text


def test_unknown_platform_only_warns(monkeypatch, caplog):
    monkeypatch.setattr(ka.sys, "platform", "linux")
    with caplog.at_level(logging.WARNING):
        with ka.keep_awake():
            pass
    assert "無法防止電腦睡眠" in caplog.text
