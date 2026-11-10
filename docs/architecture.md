# Architecture

One gateway process, or several sharing Postgres and Valkey. Solid arrows are the request path;
dotted arrows are observation and background work. The module behind each box is in
[CLAUDE.md](../CLAUDE.md#architecture); each step's reasoning is in its numbered note
([index](README.md)).

```mermaid
flowchart TD
    client(["Client with an API key"]) -->|"POST /v1/generate<br/>POST /v1/chat/completions"| api
    person(["Person in a browser"]) -->|"/dashboard (monitoring)<br/>/dashboard/console.html (admin)"| api
    prom(["Prometheus"]) -->|"GET /metrics"| api

    subgraph gw["Gateway process (local only until A8: loopback peers, no forwarded headers)"]
        api["HTTP API: FastAPI, app.py is the only wiring point"]
        auth["Keys and identity<br/>API keys as HMACs, sessions and CSRF,<br/>roles and authorize(), step-up, audit log"]
        settings["Settings<br/>platform, plan, organization, application:<br/>the stricter wins; read with the key"]
        router["Routing<br/>plan, tier, model name or quality"]
        budgets["Limits<br/>rate limit, token budget, max_tokens"]
        chat["Chat router<br/>the mode, fallback triggers,<br/>breakers, a cap on hosted attempts (3 by default)"]
        workers["Workers, one per model<br/>continuous batching, prefix cache,<br/>chunked prefill, overload"]
        recorder["Alert recorder<br/>per application and minute, never awaits"]
        evaluator["Alert evaluator<br/>one holder by a fenced lease;<br/>incidents, heartbeat in /healthz"]
        outbox["Outbox and delivery worker<br/>webhooks, email to verified recipients"]
        monitor["Monitoring<br/>snapshot, SSE stream, events:<br/>watches and decides nothing"]

        api --> auth --> settings --> router --> budgets
        budgets --> workers
        budgets --> chat
        api -.-> recorder
        recorder -.-> evaluator -.-> outbox
        api -.-> monitor
        workers -.-> monitor
    end

    subgraph backends["Model backends"]
        sim["Simulator (default)<br/>calibrated profiles, injected clock"]
        local["llama-server or Ollama<br/>on this machine"]
    end

    subgraph egress["Outbound connections"]
        hosted["Organization providers<br/>OpenAI-compatible or Anthropic:<br/>https, public addresses, no redirects"]
        hooks["Webhook receivers<br/>signed, checked at connect"]
        smtp["The operator's SMTP server<br/>STARTTLS or implicit TLS only"]
    end

    subgraph stores["State"]
        sqlite[("SQLite: one process per database")]
        pg[("Postgres: durable stores, shared mode")]
        valkey[("Valkey: counters and rollups, shared mode")]
        vault["Vault: provider keys, channel secrets,<br/>the SMTP password, AES-256-GCM"]
    end

    workers --> sim
    workers --> local
    chat --> local
    chat --> hosted
    outbox --> hooks
    outbox --> smtp
    auth --- sqlite
    auth --- pg
    budgets --- valkey
    chat --- vault
    outbox --- vault
```

## How to read it

- **Default mode is the simulator.** No model runs; time is simulated from a cost model fitted
  once to real models on one laptop ([note 13](13-calibration.md)). With real models the model
  server does the batching, and the gateway's scheduler, prefix cache and chunked prefill are
  not in the path ([note 12](12-real-models.md)).
- **One process or several.** By default the state is one SQLite file and one process
  ([note 21](21-persistence.md)). `GATEWAY_STATE=shared` puts the durable stores in Postgres
  and the counters in Valkey, so several instances can serve together
  ([notes 34 to 38](README.md#design-notes)).
- **Monitoring decides nothing.** No scheduling, routing or admission code reads from the
  monitor, the time series or the event bus ([note 15](15-monitoring-api.md)).
- **Everything that leaves the machine goes through one guard** ([note 32](32-provider-settings-and-vault.md),
  [note 46](46-alert-channels.md)), except mail to the operator's own SMTP server, whose host
  the operator sets ([note 48](48-email-channels.md)).
