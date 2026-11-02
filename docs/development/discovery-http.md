# Provider HTTP connection ownership

The mission host supports an explicitly selected pooled discovery transport:

```sh
scripts/dev live serve --runtime mission --allow-model-calls --discovery-http httpx --state-dir .local/research
```

This command starts the live host and can resume saved provider-backed work.
It is not an offline test. Ordinary setup and checks never invoke it.

The default is `--discovery-http urllib`. HTTPX uses environment proxy and
certificate settings captured at host creation; it does not reproduce urllib's
macOS/Windows system-proxy discovery. Select it only when that environment policy
is appropriate. Restart the host after changing proxy configuration.

One HTTPX client is shared by provider discovery, WebSearch and rendering. Source
intake, model inference, private corpus retrieval and validation transports retain
their own policies. Injected transports remain caller-owned. Host shutdown stops
new pooled requests; active synchronous callbacks can finish before the pool
closes. Existing executor uncertainty and saved acceptance checks remain in force.

The adapter preserves the HTTP result shape, raw 8 MiB plus one-byte size marker,
status/Retry-After classification and explicit timeouts. It does not retain
response cookies or retry a failed request. HTTPX failures are translated to the
transport exception types understood by the current discovery code.

Redirects are handled one hop at a time, without reading intermediate bodies.
POST 307/308 responses are returned without replaying the body, as with urllib.
Same-origin POST 301/302/303 redirects become GET requests; cross-origin POST
redirects stop before sending data to the other origin. GET/HEAD redirects remain
available, but custom headers such as `x-api-key` prevent a cross-origin follow.
HTTPX manages removal of Authorization/Cookie headers on allowed GET redirects.
The adapter stops after ten redirect hops.

In a synthetic loopback comparison, 250 sequential urllib requests used 250
connections while one HTTPX client reused one. HTTPX was slightly slower locally;
remote-provider latency and throughput have not been established. Connection reuse
is the demonstrated benefit, not faster model inference or a measured end-to-end
research speedup.

Offline checks use only local synthetic servers:

```sh
.venv/bin/python -m pytest -q tests/retrieval/test_http_transport.py tests/mission/test_http_transport_lifecycle.py
```

These cover HTTP results, raw gzip/oversize responses, redirects, environment
proxies, concurrency, errors, cookies and closing while a callback is active.
They do not establish real-provider accounting or platform system-proxy parity.
