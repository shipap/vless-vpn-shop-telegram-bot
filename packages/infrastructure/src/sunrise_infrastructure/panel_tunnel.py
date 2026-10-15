"""A loopback SSH forward with the original HTTPS certificate and SNI identity."""

import ssl

import httpx


class PanelTunnelTransport(httpx.BaseTransport):
    def __init__(self, hostname, port, *, verify=True, delegate=None):
        if (
            not isinstance(hostname, str)
            or not hostname
            or any(char in hostname for char in "/:@* \\")
            or any(ord(char) <= 32 for char in hostname)
            or type(port) is not int
            or not 1024 <= port <= 65535
        ):
            raise ValueError("panel_tunnel_configuration_denied")
        if verify is not True and not (
            isinstance(verify, ssl.SSLContext)
            and verify.check_hostname
            and verify.verify_mode == ssl.CERT_REQUIRED
        ):
            raise ValueError("panel_tunnel_tls_required")
        self.hostname, self.port = hostname, port
        self.delegate = delegate or httpx.HTTPTransport(verify=verify, trust_env=False, retries=0)

    def handle_request(self, request):
        url = request.url
        if url.scheme != "https" or url.host != self.hostname or url.port not in {None, 443} or url.userinfo:
            raise ValueError("panel_tunnel_target_denied")
        headers = request.headers.copy()
        headers["Host"] = self.hostname
        forwarded = httpx.Request(
            request.method,
            url.copy_with(host="127.0.0.1", port=self.port),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self.hostname},
        )
        return self.delegate.handle_request(forwarded)

    def close(self):
        self.delegate.close()
