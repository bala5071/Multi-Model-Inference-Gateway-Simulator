# 25 — Local only: the guard for unauthenticated endpoints, and the launcher

Step 2 of the identity plan (`docs/identity-plan.md`, section 10). Monitoring, metrics, the
overload view, load control, the Playground and the dashboard still have no user login (the A8
role-based monitoring step adds it). Until then they are served only to this machine, and that
is now enforced inside the app, not only by how it is started.

Code: `src/gateway/localonly.py` (the guard, an ASGI middleware), `src/gateway/serve.py` (the
launcher), wiring in `app.py`. Tests: `tests/test_local_only.py`, `tests/test_deployment_config.py`.

## The guard

On `/v1/monitor`, `/metrics`, `/v1/overload`, `/v1/control`, `/v1/playground` and `/dashboard`
(each path and everything under it), a request is refused when:

| Check | Refused with |
|---|---|
| The socket peer is not loopback (`127.0.0.0/8`, `::1`, or IPv4-mapped `::ffff:127.x`) nor in `GATEWAY_LOCAL_NETWORKS` | 403 `local_only` |
| It carries `Forwarded`, `X-Forwarded-For` or `X-Real-IP` | 403 `local_only` |
| Its `Host` is not `localhost`, `127.0.0.1`, `[::1]` (any port) or a name in `GATEWAY_ALLOWED_HOSTS` | 403 `host_not_allowed` |
| It changes state (POST, PUT, PATCH, DELETE) and has an `Origin` whose host is not allowed (`null` included) | 403 `origin_not_allowed` |
| It changes state with a `text/plain`, form or multipart body | 415 `unsupported_media_type` |

Every other endpoint (generate, models, health, an application's own usage) is untouched.

- **Peer address only.** The guard reads the ASGI `client`, never a header. A request with any
  forwarding header is refused outright, because its client address may already have been
  rewritten: plain `uvicorn` does that by default, trusting `X-Forwarded-For` from
  `127.0.0.1`. `/v1/playground` is on the list although section 10 did not name it: it sends
  requests as an application using keys the server holds, so it must not be reachable from
  elsewhere.
- **Browser-based attacks.** A page in the user's browser can send requests to `127.0.0.1`. A
  DNS-rebound name resolves to `127.0.0.1` but keeps its own `Host`, which the allowlist
  refuses. A cross-site page can send a POST without a CORS preflight only with a
  `text/plain`, form or multipart body, or with no body; the first three are refused by type,
  and all of them carry the attacker's `Origin`, which is refused. The dashboard's own
  requests (same origin, JSON) and the Vite dev server's (it proxies with the browser's
  `localhost:5173` Host and Origin) are accepted.
- **In the app, so it holds however it is served.** Both server factories,
  `create_default_app` and `create_simulated_live_app`, always install it, with the policy read
  from the environment. Tests that build an app with `create_app` add it when they test it.
- **What it does not protect against:** anything running on this machine can still read
  monitoring and use load control; that waits for A8.

## Docker

Behind Docker (and Docker Desktop's NAT), every request from the host reaches the container
from the bridge network's gateway, so the peer address cannot tell a local client from a
remote one. There, **the protection is publishing the port on `127.0.0.1` only**:

- `docker-compose.yml` publishes `127.0.0.1:8000:8000` and sets
  `GATEWAY_LOCAL_NETWORKS=172.16.0.0/12`, so the guard counts the bridge network as local.
  That setting **trusts the published binding**: it says only this machine can reach that
  network, which holds only while the port is published on loopback.
- CI checks it twice: `docker compose config` must show every published port with
  `host_ip` `127.0.0.1`, and `docker compose port gateway 8000` on the running demo must
  answer `127.0.0.1:…`; then the dashboard must still be served through the published port.
- Only Linux Docker in CI has run this. Docker Desktop on Windows or macOS may present the
  host's requests from another range (for example `192.168.65.0/24`); if the dashboard
  answers `local_only` there, add that network to `GATEWAY_LOCAL_NETWORKS`. Not tested.

## The launcher

`python -m gateway.serve [--host H] [--port P]` starts the default server with uvicorn's proxy
headers off. It refuses to bind a non-loopback address (exit 2, naming the open endpoints)
unless `GATEWAY_ALLOW_NETWORK_BIND=1`, in which case it starts and prints a warning naming
them. The Docker image sets the opt-in, because it must bind `0.0.0.0` inside the container.
The README quickstart, the Makefile, the Dockerfile and the browser tests' configuration now
start the server this way; the readme-quickstart CI job runs the new command literally.

Running `uvicorn gateway.app:create_default_app --factory` directly still works and is still
safe: a test starts a real `uvicorn` process that way (proxy headers on, as uvicorn defaults)
and finds a clean local request served and each forwarding header and a rebound Host refused,
exactly as through the launcher.
