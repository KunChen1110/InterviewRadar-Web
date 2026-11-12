from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_nginx_overwrites_forwarding_chain_and_has_upstream_timeouts():
    for filename in ("deploy/nginx.conf", "deploy/nginx.https.conf"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        assert "proxy_set_header X-Real-IP $remote_addr;" in text
        assert "proxy_set_header X-Forwarded-For $remote_addr;" in text
        assert "$proxy_add_x_forwarded_for" not in text
        assert "proxy_read_timeout 75s;" in text


def test_container_only_trusts_the_compose_network_for_forwarded_headers():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips=*" not in text
    assert "FORWARDED_ALLOW_IPS" in text


def test_compose_sets_the_exact_nginx_gateway_ip_for_forwarded_headers():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "172.30.0.2" in text
    assert "172.30.0.3" in text
