# RAG as a Service Architecture — Multi-Tenant

## 1. Context and Problem

Multi-tenant RAG as a Service platform. Each tenant accesses their datasources; some datasources are shared among multiple tenants. Currently there are 3 services with public access, and the RAG service is 100% coupled to Azure AI Search. The goals are:

- Separate external and internal traffic with 2 BFFs (Client / Admin).
- Authentication via external Entra ID; internal JWT for services.
- 100% internal services.
- Refactor endpoints with context awareness.
- Allow each tenant/datasource to use different query and embedding algorithms, **and be able to compare them** (experimentation / evaluation).
- Keep embedding/ingestion pipelines in the RAG service.

---

## 2. High-Level Diagram

```
                    ┌──────────────┐        ┌──────────────┐
                    │  Client App   │        │  Admin Panel  │
                    └──────┬───────┘        └──────┬───────┘
                           │ Entra ID (OAuth2)     │ Entra ID (OAuth2)
                    ┌──────┴───────┐        ┌──────┴───────┐
                    │  BFF Client  │        │  BFF Admin   │
                    │ (passthrough)│        │ (passthrough)│
                    │  no DB       │        │  no DB       │
                    └──────┬───────┘        └──────┬───────┘
                           │                       │
══════════════════════════╪═══════════════════════╪══════════════════════
        Internal Network Only — REST/gRPC + mTLS + internal JWT
══════════════════════════╪═══════════════════════╪══════════════════════
                           ▼                       ▼
   ┌────────────────────────────────────────────────────────────────┐
   │  SYNCHRONOUS PLANE (hot path)                                    │
   │  ┌──────────────┐   ┌──────────────┐   ┌────────────────────┐  │
   │  │ Tenant       │   │   RAG        │◄──┤   Completions      │  │
   │  │ Service      │   │   Service    │   │   Service          │  │
   │  │              │   │              │tool│  (AGENT            │  │
   │  │ tenants,     │   │ datasources, │`retrieve`│ ORCHESTRATOR)│  │
   │  │ plans,       │   │ tenant_ds,   │   │ chat loop + tools, │  │
   │  │ quotas,      │   │ ingestion,   │   │ function calling,  │  │
   │  │ status,      │   │ embedding,   │   │ LLM proxy,         │  │
   │  │ onboarding,  │   │ retrieval,   │   │ tools/MCP registry │  │
   │  │ billing      │   │ retr. eval   │   │                    │  │
   │  │ ┌────┐       │   │ ┌────┐ ┌───┐ │   │ ┌────┐             │  │
   │  │ │ PG │       │   │ │ PG │ │ MQ│ │   │ │ PG │             │  │
   │  │ └────┘       │   │ ├────┤ ├───┤ │   │ └──┬─┘             │  │
   │  └──────▲───────┘   │ │ S3 │ │Vec│ │   └────┼───────────────┘  │
   │         │           │ └────┘ └───┘ │        │ tenant tools     │
   │         │           └──────────────┘        ▼ (MCP / HTTP)     │
   │         │                            ┌──────────────────┐      │
   │         │                            │ MCP servers /    │      │
   │         │                            │ Tenant APIs      │      │
   │         │                            └──────────────────┘      │
   │  ═══════╪═══════════════ EVENT BUS ═══════════════════════════ │
   │         │   usage.recorded │ conversation.completed │ retrieval.executed
   │         │ (consume)        ▼ (consume)               ▼          │
   │         │           ┌────────────────────────────────────────┐ │
   │  (Tenant◄┘          │        Evaluation Service              │ │
   │   consume usage)    │  ASYNCHRONOUS PLANE (offline, outside  │ │
   │                     │  hot path): datasets, evaluators       │ │
   │                     │  (LLM-as-judge), retrieval + agent     │ │
   │                     │  metrics, experiments                  │ │
   │                     │  ┌────┐                                 │ │
   │                     │  │ PG │                                 │ │
   │                     │  └────┘                                 │ │
   │                     └────────────────────────────────────────┘ │
   │                                                                │
   │  ┌──────────────────────────────────────────────────────────┐ │
   │  │  Observability: OpenTelemetry → Jaeger + Prometheus + Grafana │
   │  └──────────────────────────────────────────────────────────┘ │
   └────────────────────────────────────────────────────────────────┘
```

**4 internal services + Event Bus + 2 BFFs + Entra ID.** The BFFs are thin and stateless. The **tenant** entity lives in its own domain service (Tenant Service), not in a BFF nor in Entra. **The Completions Service is an agent orchestrator with an extensible toolset**: the BFF Client only forwards the conversation, and it's the LLM (via Completions) who decides which tool to use. RAG (`retrieve`) is **just another tool**; the tenant can register others (MCP/HTTP). Data access control always remains in the RAG, and each tool is only exposed to the tenant that configured it.

**Separation of planes:** the user response path (hot path) is 100% synchronous. Everything that should not be in that path —quality evaluation and billing— is decoupled via an **Event Bus**: services publish usage events and traces, and **Evaluation Service** (quality) and **Tenant Service** (billing) consume them asynchronously. This way Completions does not bloat with those responsibilities.

---

## 2.1 Event Bus — asynchronous backbone

The Event Bus decouples everything that **should not** happen in the hot path (billing, quality evaluation) from the user response. Producers publish and move on; consumers process at their own pace. If a consumer goes down, events accumulate and are processed upon recovery: **the user's conversation is never affected**.

**Events (topics):**

| Event | Publishes | Consumes | Purpose |
|-------|-----------|----------|---------|
| `usage.recorded` | Completions (LLM tokens, tool calls), RAG (queries, ingested docs) | **Tenant Service** | Billing and quota enforcement. |
| `conversation.completed` | Completions | **Evaluation Service** | Full agent trace for end-to-end eval. |
| `retrieval.executed` | RAG | **Evaluation Service** | Result of each `retrieve` (chunks, scores, strategy, version) for retrieval eval. |

**Event shape (example `conversation.completed`):**
```jsonc
{
  "event": "conversation.completed",
  "event_id": "uuid",              // for consumer idempotency
  "tenant_id": "abc",              // from JWT, not from the LLM
  "conversation_id": "uuid",
  "occurred_at": "2026-06-01T10:00:00Z",
  "model": "gpt-4o",
  "tool_calls": [                  // which tools the agent chose and results
    { "name": "retrieve", "args": {...}, "latency_ms": 120, "result_ref": "..." },
    { "name": "jira_tickets.search_issues", "args": {...}, "latency_ms": 340 }
  ],
  "rounds": 2,
  "tokens": { "prompt": 1500, "completion": 320 },
  "final_answer_ref": "..."        // reference to the answer (no PII inline if applicable)
}
```

**Guarantees and design:**
- **Idempotency:** each event carries `event_id`; consumers deduplicate. *At-least-once* semantics.
- **Isolation:** `tenant_id` travels in the event, taken from the JWT at origin — never from what the LLM says.
- **No unnecessary PII:** large payloads (responses, chunks) travel by reference to object store when appropriate; the event carries metadata.
- **Order:** partitioned by `tenant_id`/`conversation_id` when the consumer needs per-conversation ordering.
- **Technology:** NATS JetStream or Kafka (see §9). Persistent with retries/DLQ.

---

## 3. Authentication

### 3.1 Entra ID

User authentication (clients and admins) is the exclusive responsibility of Entra ID. Entra ID groups are mapped to tenants and roles:

```
Group "tenant-abc-users"     → client users of tenant abc
Group "tenant-abc-admins"    → administrators of tenant abc
```

The BFFs validate the Entra ID token using `MSAL` / `azure-identity`. The group→`tenant_id` mapping and the tenant's existence/status are resolved by the **Tenant Service** (it is not assumed that the group *is* the tenant).

### 3.2 BFFs: from external token to internal JWT

Both BFFs are **stateless, no DB**. Common flow:

1. Validate the user's Entra ID token.
2. Resolve `tenant_id` from the token's groups, querying the **Tenant Service** (which is the source of truth for group↔tenant mapping and tenant status).
3. Issue an **internal JWT** (RS256 signed, rotatable keys via JWKS) with limited claims:

```json
{
  "sub": "user-uuid-from-entra",
  "tenant_id": "abc",
  "scope": "admin",
  "iat": 1717200000,
  "exp": 1717200600
}
```

**Do not include `datasources` in the JWT.** That is validated by the RAG Service against its own DB on each request.

> If the tenant is suspended or over quota, the Tenant Service indicates it and the BFF cuts the flow before issuing the internal JWT.

### 3.3 BFF Client

- Exposes public REST API for client apps.
- Validates Entra ID token → resolves tenant via Tenant Service → issues internal JWT with scope `client`.
- Single responsibility: **forward the conversation to the Completions Service** and **proxy the stream** back to the client.
- **Does not talk to the RAG nor decide anything about context.** Completions orchestrates retrieval (see §4.3 and §5). No business logic, no double hop.

### 3.4 BFF Admin

- Exposes REST API for the admin panel.
- Validates Entra ID token → group `*-admins` → issues internal JWT with scope `admin`.
- Single responsibility: **forward/orchestrate** panel requests to the corresponding internal service (Tenant Service, RAG, or Completions). Aggregates responses when the panel needs data from multiple services.
- **Does not persist anything.** All tenant metadata lives in the Tenant Service.

### 3.5 Internal JWT Validation

Each internal service validates the JWT in a middleware. It travels in the header `Authorization: Bearer <jwt>`. Signature verification uses JWKS exposed by the issuer (BFFs), with key rotation. Internal services **do not** validate Entra ID tokens.

---

## 4. Internal Services

### 4.0 Tenant Service — owner of the tenant entity

Domain service (not anemic) responsible for the **tenant lifecycle** and its metadata. It exists because there is real business logic: onboarding, plans, quotas, suspension, Entra mapping, **and billing**. It is not a mere CRUD-DB.

**Why a service and not a BFF with a DB:** a BFF is a thin adaptation layer for a frontend, stateless and without domain. Putting the tenant entity in it would turn it into a disguised domain service — precisely the coupling we want to avoid. The tenant entity has its own domain → its own service.

**Owner of billing.** Billing lives here, not in Completions. The Tenant Service already knows plan, quotas, and status; consolidating usage is its natural responsibility. It does **not** measure usage in the hot path: it **consumes it from the Event Bus** (`usage.recorded` event published by Completions and RAG) asynchronously. This way Completions only *emits* usage events and stays free of billing logic.

**Own infrastructure:** PostgreSQL. Event Bus consumer (`usage.recorded`).

**PostgreSQL schema:**
```
tenants            → id, name, entra_group_users, entra_group_admins, plan, status (active|suspended), created_at
tenant_quotas      → tenant_id, max_datasources, max_queries_month, max_tokens_month, max_docs, ...
usage_events       → event_id (PK, idempotency), tenant_id, source (completions|rag),
                     kind (llm_tokens|tool_call|query|ingest), quantity, occurred_at
tenant_usage       → tenant_id, period, queries_count, tokens_count, tool_calls_count, ...  (aggregated for enforcement)
billing_records    → tenant_id, period, line_items_json, total, status (open|invoiced)
```

**API:**
```
# Onboarding and management (called by BFF Admin)
POST   /tenants                       # create tenant, associate Entra groups, initial plan
GET    /tenants/{tid}
PUT    /tenants/{tid}                  # change plan, status, quotas
DELETE /tenants/{tid}

# Resolution used by BFFs during login
POST   /tenants/resolve                # body: { "entra_groups": [...] } → { tenant_id, scope, status }

# Quota enforcement (queried by BFF Client before orchestrating; optional in hot path)
GET    /tenants/{tid}/quota-status     # { within_quota: bool, reason: ... }

# Billing (queried by BFF Admin)
GET    /tenants/{tid}/usage            # aggregated usage for the period
GET    /tenants/{tid}/billing          # billing_records for the tenant
```

**Billing pipeline (async, via Event Bus):**
1. Completions and RAG publish `usage.recorded` (LLM tokens, tool calls, queries, ingests) after each operation.
2. Tenant Service consumes the event, **deduplicates by `event_id`** (idempotency; the bus is at-least-once) and persists it in `usage_events`.
3. Aggregates into `tenant_usage` per period → feeds `quota-status` and, at period close, generates `billing_records`.
4. If usage exceeds quota, marks the tenant so that `/tenants/resolve` and `quota-status` reflect the limit (enforcement on next login/query).

> **Consistency decision:** quota enforcement is *eventually consistent* (usage arrives via the bus, not on the hot path). This is a conscious trade-off for latency: a tenant might slightly exceed its quota before the system reacts. Acceptable for consumption-based billing.

---

### 4.1 RAG Service — owner of datasources and tenant↔datasource relationships

It is the core of the refactor. It decouples from Azure AI Search using **Strategy + Adapter**. It has three responsibilities: **Ingestion**, **Query**, and **Evaluation**.

**It is the natural owner of datasources and who can access them.** Each query that reaches the RAG validates that the `tenant_id` from the JWT has access to the `datasource_id`(s). Having this information locally avoids extra calls in the hot path.

**Own infrastructure:**
- PostgreSQL: datasources, tenant_datasources, embedding versions, pipeline status, experiments.
- Message Queue: async ingestion and re-indexing jobs.
- Object Store (S3/MinIO): raw documents (key for re-indexing without re-upload).
- Vector Stores: one collection per **(datasource, embedding version)**.

**PostgreSQL schema:**
```
datasources           → id, name, owner_tenant_id, chunk_config,
                        active_embedding_version_id, active_retrieval_strategy
embedding_versions    → id, datasource_id, embedding_strategy, dimension,
                        vector_store_config, status (building|active|deprecated), created_at
tenant_datasources    → tenant_id, datasource_id, permission (read|write|admin)
pipeline_jobs         → id, datasource_id, document_id, embedding_version_id,
                        kind (ingest|reindex), status, created_at, ...
documents             → id, datasource_id, object_store_key, checksum, created_at
experiments           → id, datasource_id, name, control_version_id,
                        candidate_version_id|candidate_retrieval, mode (ab|shadow), traffic_pct, status
                        # ONLY hot path routing config. Metrics/verdict live in Evaluation Service (§4.4).
```

**API:**
```
# Datasource configuration (called by BFF Admin)
POST   /datasources
PUT    /datasources/{ds_id}
DELETE /datasources/{ds_id}

# Tenant↔datasource assignment (called by BFF Admin)
POST   /datasources/{ds_id}/tenants/{tenant_id}    # body: { "permission": "read" }
DELETE /datasources/{ds_id}/tenants/{tenant_id}
GET    /datasources/{ds_id}/tenants
GET    /tenants/{tenant_id}/datasources

# Ingestion
POST   /datasources/{ds_id}/documents
DELETE /datasources/{ds_id}/documents/{doc_id}

# Embedding versioning and re-indexing
POST   /datasources/{ds_id}/embedding-versions        # create new version (triggers async reindex)
POST   /datasources/{ds_id}/embedding-versions/{v}/activate   # atomic active version swap
GET    /datasources/{ds_id}/embedding-versions

# Query — invoked by Completions Service as `retrieve` tool
POST   /tenants/{tid}/query/context
  Headers: Authorization: Bearer <internal-tenant-jwt>   # propagated by Completions
  Body: { "query": "...", "top_k": 10, "filters": {...}, "datasource_ids": null }
  # datasource_ids optional; if null, the RAG searches ALL datasources accessible by the tenant.
  # The RAG ALWAYS validates access against tenant_datasources using the JWT's tenant_id,
  # regardless of what the LLM requests. The model CANNOT expand its own scope.
  Response: { "chunks": [...], "scores": [...], "metadata": { "datasources": [...], "experiment": ... } }

# Direct query to a specific datasource (admin / debugging use)
POST   /datasources/{ds_id}/query/context

# Experiment routing (called by BFF Admin) — only activate/configure A-B or shadow
POST   /datasources/{ds_id}/experiments               # define A/B or shadow (routing)
DELETE /datasources/{ds_id}/experiments/{id}
# Datasets, metrics, and results are managed in the Evaluation Service (§4.4), not here.

# Status
GET    /datasources/{ds_id}/status
```

**Access validation on each query:**

The RAG receives a JWT with `tenant_id`. Before executing:
1. Resolves the accessible `datasource_ids` from `tenant_datasources` (or validates the received ones).
2. If no access, returns 403.
3. Proceeds with the query. **No external call. No extra latency.**

#### 4.1.1 Decoupled Pipelines

```
┌──────────────────────────────────────────────────────────────────┐
│                          RAG Service                             │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │               IngestionPipeline (async via MQ)          │   │
│  │  Document ─► Loader ─► Splitter ─► Embedder ─► VectorStore │   │
│  │  • Loader: based on type (PDF, HTML, TXT, API)          │   │
│  │  • Splitter: datasource chunk_config                     │   │
│  │  • Embedder: EmbeddingStrategy of the embedding_version   │   │
│  │  • VectorStore: Adapter, collection (datasource, version) │   │
│  │  • Raw saved to S3 → enables re-indexing without re-upload│   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │               ReindexPipeline (async via MQ)            │   │
│  │  New embedding_version ─► re-reads S3 ─► re-embed ─►      │   │
│  │   new collection (status=building) ─► activate (swap)    │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │               QueryPipeline                              │   │
│  │  Query ─► Validate access ─► [Experiment router] ─►       │   │
│  │   Embedder(version) ─► Retriever ─► Post-process ─► Result│   │
│  │  • Experiment router: decides control/candidate or shadow │   │
│  │  • Embedder: from the active embedding_version            │   │
│  │  • Retriever: RetrievalStrategy of the datasource         │   │
│  │  • Post-processing: reranking, filtering, fusion          │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

#### 4.1.2 EmbeddingStrategy (Interface)

```python
class EmbeddingStrategy(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...
    @property
    def dimension(self) -> int: ...

# Implementations:
# - OpenAIEmbedding(model="text-embedding-3-small")
# - CohereEmbedding(model="embed-multilingual-v3")
# - SentenceTransformerEmbedding(model="intfloat/multilingual-e5-large")
```

#### 4.1.3 RetrievalStrategy (Interface)

```python
class RetrievalStrategy(Protocol):
    async def retrieve(
        self,
        query_vector: list[float],
        datasource_ids: list[str],
        top_k: int,
        filters: dict | None,
    ) -> list[RetrievedChunk]: ...

# Implementations:
# - SemanticSearch
# - HybridSearch (vector + BM25)
# - MultiStageRetrieval (coarse + reranker)
# - ContextualRetrieval
# - GraphRAG
```

#### 4.1.4 VectorStore (Adapter)

```python
# Adapters: AzureAISearchAdapter, QdrantAdapter, WeaviateAdapter, PineconeAdapter
# Each collection/index is named by (datasource_id, embedding_version_id)
```

#### 4.1.5 Embedding Versioning and Re-indexing

The embedding, vector dimension, and vector store collection are **physically coupled**: a change in the embedding model invalidates all existing vectors. That is why the versioned unit is not the datasource but the **embedding_version**.

Rules:
- The **query** embedder is ALWAYS the one from the active `embedding_version` used during ingestion. Dimensions are not mixed.
- Changing strategy/model = creating a new **embedding_version** → `ReindexPipeline` re-reads raw documents from S3, re-embeds into a **new collection** (`status=building`).
- When done, `activate` performs an **atomic swap** of the active version. Zero downtime; the old version becomes `deprecated` and can be deleted.
- This is what makes it safe to "try another embedding": you create a candidate version without touching production.

---

### 4.2 Experimentation in the RAG (routing) + Evaluation (delegated)

The stated goal is **to be able to test and compare** algorithms from a tenant. Here we need to separate two responsibilities:

- **Experiment routing (hot path, in the RAG):** deciding which branch (control/candidate) serves each query. It is synchronous and lives in the RAG because it is the one executing `retrieve`.
- **Metrics calculation and verdict (offline, in the Evaluation Service):** comparing branches, running datasets, judging quality. Does **not** live in the RAG; it is delegated to the Evaluation Service (§4.4) via the Event Bus.

**Modes:**

| Mode | Description | Routing | Metrics |
|------|-------------|---------|---------|
| **Offline eval** | Dataset (queries + expected relevance) against one or more versions/strategies. | — (no real traffic) | Evaluation Service: recall@k, nDCG, MRR, latency |
| **Shadow** | The real query is executed against control and candidate; the user only gets control. | RAG executes both branches | Evaluation Service compares both from `retrieval.executed` |
| **A/B** | A % of traffic (`traffic_pct`) goes to candidate; the rest to control. | RAG Experiment router | Evaluation Service per branch |

- The **Experiment router** in the QueryPipeline (RAG) decides the branch according to the `experiments` table and **publishes `retrieval.executed`** to the Event Bus with the branch, strategy, version, chunks, and scores.
- The **Evaluation Service** consumes those events, calculates metrics, and exposes results to the panel (via BFF Admin).
- "Test from a tenant" = the tenant admin creates an experiment on their datasource; that tenant's traffic feeds the comparison. No need to duplicate datasources.

---

### 4.3 Completions Service — agent orchestrator with tools

It is the **agent orchestrator**. It wraps different LLMs and runs a **chat loop with function calling**: at each turn, the model decides which **tool** to use (if any). Retrieving context from the RAG (`retrieve`) is **just another tool**, not the only path.

**Mental model:** it is not "a RAG with an LLM on top", but "an agent with a toolset, where our RAG is one of the tools". This is deliberate to allow extension: a tenant may not always need context from our databases; it may also want other capabilities.

**Toolset per tenant (extensible):**
- `retrieve` — RAG retrieval (platform tool, always available if the tenant has datasources).
- **Configurable tenant tools** — the tenant can register additional tools, typically their own **MCP servers** (internal APIs, web search, ticket systems, etc.).

At the start of each conversation, Completions resolves the **effective toolset for the tenant** (platform + configured ones) and declares them to the LLM. The LLM chooses; Completions executes and re-injects results. Adding a new capability does not touch the flow: it is registering another tool.

**Why here and not in the BFF:** the BFF should be thin. The decision of *which tool to use and when* is agent logic, belonging to the component that talks to the LLM. Putting it in Completions keeps the BFF as a simple proxy and enables a truly extensible agent.

**Own infrastructure:** PostgreSQL for per-tenant prompt templates, model configuration, and **tools/MCP registry per tenant**. Event Bus producer (`usage.recorded`, `conversation.completed`). **Does not persist billing**: it emits usage events and the Tenant Service bills.

**Tool `retrieve` (platform → RAG):**
```jsonc
{
  "name": "retrieve",
  "description": "Retrieves relevant context from the tenant's knowledge base.",
  "parameters": {
    "query": "string",            // search text decided by the model
    "top_k": "integer",
    "datasource_ids": "string[]?" // OPTIONAL and only a suggestion; the RAG validates it
  }
}
```

When the LLM invokes `retrieve`, Completions does:
```
POST /tenants/{tid}/query/context  to the RAG
  Authorization: Bearer <same internal tenant JWT>   # propagated, NOT re-issued by the LLM
```

**Tenant tools (MCP / external):** they are executed through a common **tool adapter** (Strategy/Adapter, same idea as in the RAG). The registry stores the type (`mcp`, `http`, …), endpoint, and credentials per tenant. Guarantees:
- A tenant's tool is **only** offered in that tenant's conversations (isolation by JWT `tenant_id`).
- Tool credentials live in the tenant's registry, **not** chosen or seen by the LLM.
- Every tool invocation is counted for usage/billing and traced (OpenTelemetry).

**Tool registry schema (Completions PostgreSQL):**
```
tenant_tools   → id, tenant_id, name, type (mcp|http), config_json,
                 credentials_ref (secret manager), enabled, created_at
```

**Example: configuring an MCP server for a tenant.** The tenant admin registers, via BFF Admin, their own MCP server (e.g., an internal ticket system):

```jsonc
// POST /tenants/{tid}/tools  (BFF Admin → Completions)
{
  "name": "jira_tickets",
  "type": "mcp",
  "config": {
    "transport": "http",
    "url": "https://mcp.tenant-abc.com/sse",     // tenant's MCP server
    "tools_allowlist": ["search_issues", "get_issue"]  // which MCP tools to expose to the LLM
  },
  "credentials_ref": "secret://tenant-abc/jira-mcp-token"  // credential, NOT visible to the LLM
}
```

When starting a conversation for that tenant, Completions:
1. Connects to the MCP server (MCP client), discovers its tools, and filters by `tools_allowlist`.
2. Declares to the LLM the toolset = `retrieve` (platform) + `search_issues`, `get_issue` (from the tenant's MCP).
3. If the LLM calls `search_issues(...)`, the MCP adapter executes the call using `credentials_ref` resolved from the secret manager, and injects the result as a `tool` message.

Another tenant with a different MCP (or none) sees a different toolset. Adding a new capability = registering another tool, without touching code or flow.

**Isolation guarantee (key):** the `tenant_id` comes **always** from the JWT, never from what the model writes. This applies to both `retrieve` (the RAG validates against `tenant_datasources`) and tenant tools/MCP (only the tenant's tools with their credentials are exposed). The LLM decides *which* tool to call and with what *business arguments*, but it **cannot** choose tenant, credentials, MCP servers, or tools outside its scope (defense against prompt injection).

**API:**
```
# Chat (called by BFF Client)
POST   /chat
  Headers: Authorization: Bearer <internal-jwt>   # tenant_id goes here, not in the body
  Body: {
    "model": "gpt-4o",
    "messages": [...],          // full user conversation
    "stream": true
  }

# Tools/MCP management per tenant (called by BFF Admin)
POST   /tenants/{tid}/tools          # register a tool (type: mcp|http)
GET    /tenants/{tid}/tools          # list tenant's configured tools
PUT    /tenants/{tid}/tools/{id}     # edit / enable-disable
DELETE /tenants/{tid}/tools/{id}
```

Internal per-turn flow:
1. Resolves the **tenant's effective toolset** (platform + configured tools/MCP) and builds the prompt (per-tenant template).
2. Calls the LLM. If the LLM issues a `tool_call` → Completions routes it:
   - `retrieve` → RAG (propagating JWT).
   - tenant tool → MCP/HTTP adapter with the registry's credentials.
   Injects the result as a `tool` message and calls the LLM again.
3. Repeats until final answer or until `max_tool_rounds` (iteration limit to bound latency/cost).
4. Returns **streaming** (see §5.1).
5. Upon conversation close, **publishes to the Event Bus** (outside the hot path): `usage.recorded` (tokens + tool calls) → consumed by Tenant Service for billing/quota, and `conversation.completed` (agent trace) → consumed by Evaluation Service.

---

### 4.4 Evaluation Service — quality, async

Internal service **outside the hot path**. It consumes from the Event Bus the traces published by RAG (`retrieval.executed`) and Completions (`conversation.completed`) and calculates quality at two levels. Its existence prevents Completions and RAG from bloating with evaluation logic: they only *emit* what happened; the Evaluation Service *judges*.

**Two evaluation levels:**

| Level | What it measures | Source | Metrics |
|-------|-----------------|--------|---------|
| **Retrieval** | Isolated `retrieve` quality | `retrieval.executed` (RAG) | recall@k, nDCG, MRR, latency |
| **Agent (end-to-end)** | Full conversation quality | `conversation.completed` (Completions) | correct tool-choice, resolution rate, groundedness/faithfulness, number of rounds, e2e latency, cost |

**Evaluators (Strategy/Adapter):**
- **Dataset with ground-truth**: for offline eval with expected relevance/answers.
- **LLM-as-judge**: to judge answer quality or relevance when there is no ground-truth (groundedness, usefulness). Uses the LLM Gateway / Completions like any other client.
- **Heuristics/feedback**: user thumbs up-down, implicit signals.

**Own infrastructure:** PostgreSQL. Event Bus consumer.

**PostgreSQL schema:**
```
eval_datasets     → id, tenant_id, scope (retrieval|agent), items_json, created_at
experiments       → id, tenant_id, target (retrieval|agent), name,
                    control_ref, candidate_ref, mode (offline|shadow|ab), traffic_pct, status
eval_runs         → id, experiment_id|dataset_id, metrics_json, sample_size, created_at
traces            → event_id (PK, idempotency), tenant_id, kind (retrieval|conversation), payload_ref
```

**API (queried by BFF Admin):**
```
POST   /datasets                         # upload evaluation dataset (retrieval or agent)
POST   /experiments                      # define experiment (retrieval or agent)
GET    /experiments/{id}/results         # per-branch metrics
POST   /eval-runs                        # trigger an offline eval against a dataset
GET    /eval-runs/{id}
```

**Why a separate async service:**
- Evaluation (especially LLM-as-judge) is **expensive and slow**: it must never be in the user response path.
- It unifies both levels (retrieval and agent) under a single owner, instead of spreading them across RAG and Completions.
- Decoupled by the bus: if it goes down, events accumulate; nothing in the hot path breaks.

> The experiment defines *what* to compare and where traffic is routed (routing in RAG for retrieval, in Completions for agent via prompt variants/tool policy). The Evaluation Service is the one that **measures and adjudicates**. Promotion of the winning branch is executed by the owning service (RAG `activate`, or Completions changing the agent config).

---

## 5. Conversation Lifecycle

```
1.  Client       → POST /api/v1/chat {"messages": [...]}
2.  BFF Client   → Validates Entra ID token
3.  BFF Client   → Tenant Service: /tenants/resolve (groups→tenant_id, status, quota)
4.  BFF Client   → If suspended/over quota → 403/429. If OK, issues internal JWT {tenant_id, scope:"client"}
5.  BFF Client   → POST /chat to Completions Service (passthrough; carries the JWT, stream:true)
6.  Completions  → Resolves tenant toolset (retrieve + configured tools/MCP) + prompt → calls the LLM
7.  LLM          → Needs a tool? If yes → emits tool_call (e.g. retrieve(...) or an MCP tool)
8a. Completions  → If retrieve → POST /tenants/{tid}/query/context to the RAG (propagates the SAME JWT)
                   RAG validates tenant_datasources → Experiment router → embed → retrieve → chunks
8b. Completions  → If tenant tool → MCP/HTTP adapter with tenant registry credentials
9.  Completions  → Injects result as tool message → calls LLM again (repeats 7-9, max max_tool_rounds)
10. Completions  → Final LLM answer → stream
11. BFF Client   → Proxy stream to client (SSE)
─── async, outside the hot path ───────────────────────────────────────────
12. RAG          → publishes `retrieval.executed` to the Event Bus (each retrieve)
13. Completions  → publishes `usage.recorded` + `conversation.completed` to the Event Bus
14. Tenant Svc   → consumes `usage.recorded` → aggregates usage, quota, billing
15. Eval Svc     → consumes `retrieval.executed` + `conversation.completed` → retrieval + agent metrics
```

**Key improvements vs. previous version:**
- The BFF Client no longer orchestrates nor talks to the RAG: it only forwards to Completions and proxies the stream. Zero business logic.
- Completions is an **agent with extensible toolset**: the RAG (`retrieve`) is just another tool; the tenant can add MCP/HTTP tools without touching the flow.
- Access control remains **intact**: `tenant_id` comes from the JWT; the RAG validates datasources and Completions only exposes the tenant's tools.
- **Steps 1-11 are the synchronous hot path; 12-15 are async via the Event Bus.** Billing and evaluation never add latency to the response.

### 5.1 Streaming Completions → BFF → Client

- **Transport:** SSE (Server-Sent Events) end-to-end. Completions emits SSE; the BFF Client acts as a **stream proxy** (passthrough chunk by chunk), without buffering the full response.
- **During the tool loop:** while the LLM invokes `retrieve` (steps 7-11), Completions can emit intermediate SSE events (e.g. `event: status` "searching context…") for feedback, before streaming the final tokens.
- **Cancellation:** if the client closes the connection, the BFF cancels the request to Completions (disconnect propagation → cancellation token), which in turn aborts both the LLM call and any ongoing `retrieve` to the RAG. Avoids generating (and paying for) tokens nobody consumes.
- **Backpressure:** the proxy respects the consumer's pace; with a slow client, backpressure is applied towards Completions instead of accumulating in the BFF's memory.
- **Mid-stream errors:** a terminal SSE `error` event is sent instead of cutting off abruptly, so the client can distinguish "end" from "failure".

**Admin flow (create datasource and assign it to a tenant):**

```
1. Admin      → POST /api/admin/datasources {"name": "Legal", ...}
2. BFF Admin  → Validates Entra ID token, issues internal JWT scope admin
3. BFF Admin  → POST /datasources to the RAG Service
4. BFF Admin  → POST /datasources/{ds_id}/tenants/{tid} to the RAG Service
```

**Tenant onboarding flow:**

```
1. Admin      → POST /api/admin/tenants {"name": "...", "plan": "pro", "entra_groups": {...}}
2. BFF Admin  → Internal JWT scope admin
3. BFF Admin  → POST /tenants to the Tenant Service (creates tenant, quotas, Entra mapping)
```

---

## 6. Algorithms per Tenant/Datasource and Experimentation

A datasource's configuration is split between `datasources` (stable config) and `embedding_versions` (versioned):

```sql
-- Datasource and its active embedding version
INSERT INTO datasources (id, name, owner_tenant_id, chunk_config,
                         active_embedding_version_id, active_retrieval_strategy)
VALUES ('ds-42', 'Legal Knowledge Base', 'tenant-legal',
        '{"size": 512, "overlap": 64}', 'ev-1',
        '{"type": "hybrid", "vector_weight": 0.7, "bm25_weight": 0.3, "reranker": "cohere"}');

INSERT INTO embedding_versions (id, datasource_id, embedding_strategy, dimension, vector_store_config, status)
VALUES ('ev-1', 'ds-42',
        '{"type": "openai", "model": "text-embedding-3-large"}', 3072,
        '{"type": "qdrant", "url": "http://qdrant:6333", "collection": "ds_42_ev1"}', 'active');
```

**To test a different algorithm from a tenant (without duplicating datasources):**
1. Create a candidate `embedding_versions` (`ev-2`) → it is built in a new collection via re-indexing.
2. Create an `experiment` (shadow or A/B) between `ev-1` (control) and `ev-2` (candidate), or between two retrieval strategies.
3. The Experiment router routes traffic; metrics are compared in `eval_runs`.
4. If the candidate wins → `activate` the version / promote the strategy.

**What a tenant configures, in summary:**
- In the **RAG**: its datasources, embedding/retrieval strategies per datasource, and experiments (routing).
- In **Completions**: its **toolset** — besides `retrieve`, its own tools/MCP (see §4.3). A tenant can operate with only RAG context, only MCP, or both; the LLM decides at runtime based on what is configured.
- In the **Evaluation Service**: its evaluation datasets and quality experiments (retrieval and agent).

---

## 7. Multi-Tenant Security

| Layer | Mechanism |
|-------|-----------|
| Client → BFF | Entra ID (OAuth2). Entra ID groups mapped to tenants + roles via Tenant Service. |
| BFF → Internal services | Internal RS256-signed JWT (rotatable JWKS). Claims: `sub`, `tenant_id`, `scope`, `iat`, `exp`. Header `Authorization: Bearer`. |
| Service → Service | Internal JWT + mTLS. |
| Tenant (status/quota) | Tenant Service validates status (active/suspended) and quota before allowing the flow. |
| RAG (datasource isolation) | Validates `tenant_id` from JWT against `tenant_datasources` in its own DB. No external call. |
| Completions (tool loop) | The LLM decides *which* tool to use and with what arguments, but **not** the tenant: Completions propagates the JWT without re-issuing it. The RAG validates the same way. Defense against prompt injection. |
| Completions (tenant tools/MCP) | Only the JWT tenant's tools are exposed. Credentials in secret manager (`credentials_ref`), never visible to the LLM. `tools_allowlist` per MCP. |
| Event Bus | Each event carries `tenant_id` (from the JWT at origin) + `event_id` (idempotency). Consumers deduplicate. Topics/queues with per-service ACLs. |
| Vector Store | One collection per (datasource, embedding_version). Physical isolation. |

---

## 8. Responsibility Summary

| Component | Exposed | DB | Responsibilities |
|-----------|---------|----|------------------|
| **Entra ID** | External | - | User authentication. Groups = basis for tenants + roles. |
| **BFF Client** | Public | No | Validate Entra ID token, resolve tenant, issue internal JWT, forward `/chat` to Completions, stream proxy. **Does not talk to the RAG.** |
| **BFF Admin** | Public | No | Validate Entra ID token, issue internal JWT, passthrough/orchestration to Tenant/RAG/Completions. |
| **Tenant Service** | Internal | Yes | Tenant entity: metadata, plan, status, quotas, onboarding, Entra↔tenant mapping. **Billing**: consumes `usage.recorded` from the bus, aggregates usage, and generates invoices. |
| **RAG Service** | Internal | Yes | Datasources, tenant↔datasource, ingestion, embedding versioning, retrieval, experiment routing. Validates access on each query. Exposes `retrieve`. Publishes `retrieval.executed` and `usage.recorded`. |
| **Completions** | Internal | Yes | **Agent orchestrator with extensible toolset.** Chat loop, routes tools (`retrieve`→RAG, tenant tools/MCP), tool registry, prompt templates, streaming. Publishes `usage.recorded` and `conversation.completed`. **Does not persist billing.** |
| **Evaluation Service** | Internal | Yes | **Quality, async.** Consumes `retrieval.executed` and `conversation.completed`. Datasets, evaluators (LLM-as-judge), retrieval and agent metrics, experiment results. Outside the hot path. |
| **Event Bus** | Internal | - | Decouples billing and evaluation from the hot path. At-least-once, persistent, with DLQ. |

---

## 9. Suggested Stack

- **BFFs + Services**: FastAPI (Python)
- **External auth**: Entra ID (MSAL / azure-identity)
- **Internal JWT**: `pyjwt` + FastAPI middleware + JWKS endpoint (key rotation)
- **Message Queue**: RabbitMQ or NATS (async ingestion and re-indexing in the RAG)
- **Event Bus**: NATS JetStream or Kafka (usage and trace events; persistent, at-least-once, DLQ)
- **PostgreSQL**: Tenant, RAG, Completions, and Evaluation (each their own instance)
- **Vector Stores**: Azure AI Search, Qdrant, Weaviate, Pinecone
- **Streaming**: SSE end-to-end with proxy in BFF Client
- **Agent orchestration**: LLM function calling in Completions; tools routed (`retrieve`→RAG via REST/gRPC)
- **Tenant tools**: MCP (MCP client in Completions) + HTTP adapter; secrets in Key Vault / secret manager
- **Evaluation**: LLM-as-judge + ground-truth datasets, consuming traces from the Event Bus
- **Observability**: OpenTelemetry → Jaeger + Prometheus + Grafana

---

## 10. Key Decisions

1. **The tenant entity lives in a Tenant domain service, not in Entra nor in a BFF.** It has real logic (onboarding, plans, quotas, suspension, Entra mapping). A BFF with a DB would cease to be a BFF; hence its own service.
2. **Entra ID only authenticates.** Groups are the input; the mapping to `tenant_id` and the tenant's status are resolved by the Tenant Service.
3. **BFFs without DB or business logic.** They validate tokens, issue internal JWTs, passthrough. The BFF Client only forwards to Completions and proxies the stream.
4. **Completions is an agent orchestrator with an extensible toolset.** The LLM decides via function calling which tool to use. `retrieve` (RAG) is just one tool; the tenant can register others, typically their own **MCP servers**. Adding capabilities = registering tools, without touching the flow.
5. **The RAG owns datasources and the tenant↔datasource relationship.** It validates access on each query without an external call, using the JWT's `tenant_id` (never what the LLM says).
6. **Strategy + Adapter in the RAG.** Embedding and retrieval interchangeable by config; Azure AI Search decoupled.
7. **The versioned unit is the embedding_version, not the datasource.** Changing the model = new version + re-indexing from raw data in S3 + atomic swap. Zero downtime and no dimension mixing.
8. **Synchronous/asynchronous plane separation.** The hot path (user response) is 100% synchronous. Billing and evaluation are decoupled to an **Event Bus** and processed offline. They never add latency and cannot take down a conversation.
9. **Quality evaluation in its own service (Evaluation Service).** It consumes traces from the bus and measures **two levels**: retrieval and end-to-end agent. It removes evaluation (expensive and slow, e.g., LLM-as-judge) from RAG and Completions, preventing them from bloating.
10. **Billing in the Tenant Service, via events.** Completions and RAG only *emit* `usage.recorded`; the Tenant Service consumes, aggregates, and invoices. Quota is *eventually consistent* as a conscious trade-off for latency.
11. **Routing vs. measurement separated.** The owning service decides which branch to serve (routing in the hot path); the Evaluation Service measures and adjudicates (offline). Promotion is executed by the owner.
12. **End-to-end SSE streaming** with cancellation and backpressure to avoid generating tokens nobody consumes.
13. **Physical isolation in Vector Store**: one collection per (datasource, embedding_version).
14. **LLM-proof isolation**: the `tenant_id` always comes from the JWT; the model cannot expand its scope, choose a tenant, or access credentials. The RAG validates access on every `retrieve`, and Completions only exposes the tenant's tools/MCP (with secrets out of the LLM's reach).
