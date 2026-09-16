from tv_history.server import mcp


def test_import_does_not_initialize_or_download_production_history():
    from tv_history import server
    assert server.synchronizer is None


def test_busy_port_fails_before_cache_refresh(monkeypatch, capsys):
    import socket
    import pytest
    from tv_history import server
    calls = []
    monkeypatch.setattr(server, "HistorySynchronizer", lambda *a, **kw: calls.append(kw))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        monkeypatch.setattr("sys.argv", ["tv-history-mcp", "streamable-http", "--host", "127.0.0.1", "--port", str(port)])
        with pytest.raises(SystemExit) as exc:
            server.main()
        assert exc.value.code == 2
    assert calls == []
    assert "No history refresh was started" in capsys.readouterr().err


def test_free_port_probe_releases_socket():
    import socket
    from tv_history.server import check_listen_address
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    check_listen_address("127.0.0.1", port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))


def test_transport_security_uses_requested_host_allowlist() -> None:
    configured = mcp.settings.transport_security

    assert configured.enable_dns_rebinding_protection is True
    assert configured.allowed_hosts == [
        "100.123.186.70:*",
        "localhost:*",
        "127.0.0.1:*",
    ]
