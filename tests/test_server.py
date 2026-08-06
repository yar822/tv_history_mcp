from tv_history.server import mcp


def test_transport_security_uses_requested_host_allowlist() -> None:
    configured = mcp.settings.transport_security

    assert configured.enable_dns_rebinding_protection is True
    assert configured.allowed_hosts == [
        "100.123.186.70:*",
        "localhost:*",
        "127.0.0.1:*",
    ]
