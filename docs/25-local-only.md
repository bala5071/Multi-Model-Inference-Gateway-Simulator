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
| `/v1/auth` | Console login, logout and sessions (added in identity step 4, note 27); a password is needed, but logins come only from this machine |
| `/v1/admin` | Users, applications and their keys (added in identity step 7, note 30); a session, a role and step-up are needed, and only from this machine |

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

**Finding where the host's requests arrive from.** Docker Desktop may present the host's
requests from another address than Linux Docker does. The gateway's access log names the peer
of every request. With the demo running:

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
chooses the bridge network's addresses when it creates it. CI's `docker` job prints the log
line on Linux.

### What was observed on Docker Desktop

Run once, on 2026-10-22, on the tree of `7e0d79b`. The machine: Windows 11 (10.0.26200), Docker
Desktop 29.8.2 with the WSL 2 backend (kernel 6.6.87.2-microsoft-standard-WSL2), x86_64. The
compose demo was started with `docker compose up --build --detach` and a throwaway, git-ignored
`gateway.env`, which was deleted afterwards.

- **Where the host's requests come from:** `172.18.0.1`, the bridge network's gateway. The
  container was `172.18.0.2/16 via 172.18.0.1`. Both commands above gave this; the log line was
  `INFO:     172.18.0.1:32942 - "GET /v1/control HTTP/1.1" 200 OK`. The address is inside
  `172.16.0.0/12`, so the compose file's setting needs no change here.
- **Accepted from the host through `127.0.0.1:8000`, all 200:** `/healthz`, `/dashboard/`,
  `GET /v1/control`, `POST /v1/playground/generate` (JSON) and `/v1/monitor/snapshot`.
- **Published on loopback only:** `docker compose port gateway 8000` answered `127.0.0.1:8000`,
  and `netstat -ano` showed one listener for the port, `127.0.0.1:8000`, owned by Docker
  Desktop's backend.
- **Not served on the LAN address:** `curl http://192.168.1.211:8000/healthz` (the Wi-Fi
  address) could not connect (curl exit 7).
- **The shared bridge, seen from another container on the compose network:**
  `GET http://gateway:8000/v1/control` from `172.18.0.3` was refused, `403 host_not_allowed`,
  because its Host was `gateway:8000`. **The same request with `Host: localhost` was served,
  200.** So the Host allowlist does not stop a container on the bridge; any container that sets
  the header is served. That is why the setting is acceptable only for a local, single-user
  demo.
- **Cleanup:** `docker compose down -v` removed the container, the volume and the network.
  Afterwards `docker ps -a` and `docker volume ls` showed nothing from the project, and nothing
  was listening on port 8000.

**Not tested:** macOS; Docker Desktop's Hyper-V backend or any version other than 29.8.2;
WSL's mirrored networking mode, or Docker Engine run inside a WSL distribution; rootless Docker,
Podman, and Linux hosts other than CI's runner; another machine on the LAN (the LAN address was
tried only from this machine); IPv6 publishing; and the plain server (`gateway.env`, the
volume, API keys) in Docker, which has not been started.

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
