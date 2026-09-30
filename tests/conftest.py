"""The deterministic test suite must never talk to Bilibili or an AI provider."""

import socket

import pytest


@pytest.fixture(autouse=True)
def prohibit_external_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("External network access is forbidden in offline tests")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
