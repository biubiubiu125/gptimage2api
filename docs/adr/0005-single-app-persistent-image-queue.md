---
status: accepted
---

# Single App with a persistent Image Queue

`gptimage2api` runs the durable Image Task queue and its bounded Image Workers
inside the same App process that serves the public API and Vue control panel.
The Image Queue Store remains separate from the Application Database and is
managed by the Image Task runtime. One PostgreSQL service may host both logical
databases or schemas, but their repository interfaces, transaction boundaries,
and ownership remain separate.

The initial deployment does not require a Cluster, a separate image-worker
container, or worker-role admission. Queue admission, task leases, retry
backoff, recovery, and Image Worker capacity are owned by the Image Task
runtime. Registration has a separate bounded runner and runtime lease so
registration capacity is not controlled by Image Worker load.

## Consequences

- A single App deployment can absorb image bursts, enforce global/account/proxy
  concurrency, retry transient failures, and recover tasks after restart.
- The upstream asynchronous Image Task contract remains the public boundary;
  the durable queue is the one internal execution lifecycle.
- Image Task state does not move into the Application Database, preserving ADR
 0004 and the Image Task ownership described in `CONTEXT.md`.
- A future multi-process Worker split would require a new architecture
  decision; it is not part of the initial deployment.
