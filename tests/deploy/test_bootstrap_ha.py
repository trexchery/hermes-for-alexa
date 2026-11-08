

def test_http_config_ok_accepts_bare_ips_and_rejects_missing_proxy():
    from deploy.bootstrap_ha import HTTP_WANT, http_config_ok
    bare = {**HTTP_WANT, "trusted_proxies": ["172.30.77.2"]}
    assert http_config_ok(bare)
    assert not http_config_ok({**HTTP_WANT, "trusted_proxies": ["127.0.0.1"]})
    assert not http_config_ok({**HTTP_WANT, "use_x_forwarded_for": False})
    assert not http_config_ok(None)
