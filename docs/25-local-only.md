# 25 — Local only: the guard for unauthenticated endpoints, and the launcher

Step 2 of the identity plan (`docs/identity-plan.md`, section 10). Monitoring, metrics, the
overload view, load control, the Playground and the dashboard still have no user login (the A8
role-based monitoring step adds it). Until then they are served only to this machine, and that
is now enforced inside the app, not only by how it is started.

Code: `src/gateway/localonly.py` (the guard, an ASGI middleware), `src/gateway/serve.py` (the
launcher), wiring in `app.py`. Tests: `tests/test_local_only.py`, `tests/test_deployment_config.py`.

## The guard

The local-only endpoints, each path and everything under it (`LOCAL_ONLY_PATHS`; a test keeps
this table, the README's and the launcher's warning in step with it):

| Endpoint | What it gives whoever can reach it |
|---|---|
| `/v1/monitor` | The snapshot and live stream: every application's usage and spend |
| `/metrics` | Prometheus text, and the summary and detail views |
| `/v1/overload` | Each model's overload state |
| `/v1/control` | Load control: start and stop load, change the speed (when it is on) |
| `/v1/playground` | Sends requests as an application, with keys the server holds |
| `/dashboard` | The dashboard, which shows all of the above |

On these, a request is refused when:

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

Behind Docker, a request from the host reaches the container from the Docker network, not from
loopback, so the peer address cannot tell a local client from a remote one. There, **the
protection is publishing the port on `127.0.0.1` only**:

- `docker-compose.yml` publishes `127.0.0.1:8000:8000` and sets
  `GATEWAY_LOCAL_NETWORKS=172.16.0.0/12`, so the guard counts the bridge network as local.
  That setting **trusts the published binding**: it says only this machine can reach that
  network, which holds only while the port is published on loopback.
- CI checks it twice: `docker compose config` must show every published port with
  `host_ip` `127.0.0.1`, and `docker compose port gateway 8000` on the running demo must
  answer `127.0.0.1:…`; then the dashboard must still be served through the published port.

**What `172.16.0.0/12` lets in.** It is the range Docker takes its bridge networks from, and
the guard cannot tell the host's requests from a container's. So with this setting, **any
container whose address is in that range and that can connect to the gateway container** (every
container on the demo's compose network can, by the service name) can use every endpoint in the
table above: read every application's usage and spend, start load when load control is on,
and send requests through the Playground as an application. **That is acceptable only for a
local, single-user demo**, on a machine with no container you do not trust. Do not use the
setting anywhere else.

**Finding where the host's requests arrive from.** Only Linux Docker in CI has run the demo;
Docker Desktop on Windows or macOS may present the host's requests from another address. The
gateway's access log names the peer of every request. With the demo running:

```sh
curl -s http://127.0.0.1:8000/v1/control
docker compose logs gateway | grep '"GET /v1/control'
```

The first field of the last line is that address and port (uvicorn's format:
`INFO:     ADDRESS:PORT - "GET /v1/control HTTP/1.1" 200 OK`; `403` if the address is not
counted as local). The container's own network and the bridge's gateway:

```sh
docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}/{{.IPPrefixLen}} via {{.Gateway}}{{end}}' "$(docker compose ps -q gateway)"
```

If the address is outside `172.16.0.0/12`, add that one address, not a range:
`GATEWAY_LOCAL_NETWORKS: "172.16.0.0/12,ADDRESS/32"`. The narrowest setting of all would be
that one address in place of the range; the compose file uses the range because Docker
chooses the bridge network's addresses when it creates it. None of these commands has been
run on Docker Desktop; CI's `docker` job prints the log line on Linux.

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
