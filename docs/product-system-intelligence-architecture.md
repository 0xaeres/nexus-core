# Nexus Product-System Intelligence Architecture

## Executive Summary

Nexus should evolve from a product-scoped skill and RAG engine into a
product-system intelligence layer that helps coding and non-coding agents answer
questions about product geography, change impact, similar work, ownership,
debugging paths, under-documented areas, and technical debt. This should be an
extension of the current architecture, not a rewrite.

The durable design principle is: Qdrant remains the evidence retrieval layer,
SQLite remains the sync source of truth, and FalkorDB is the required
product-scoped derived graph index. FalkorDB is part of the baseline runtime
because Nexus needs reliable multi-hop topology for product-system questions.
Eval gates decide whether graph-shaped answers are good enough for default UX
confidence and promotion, not whether the graph backend exists. Current Nexus
retrieval remains deliberately simple and measured; graph traversal seeds,
filters, and explains retrieval evidence rather than replacing dense + BM25 ->
RRF -> reranker.

The key invariants do not change:

- Product is the root entity. Every node, edge, chunk, source, proposal, query,
  and cache entry carries `product_id`. Cross-product reads are tenancy bugs.
- Humans approve, agents draft. Agents may propose graph corrections, system
  insights, and skill/context artifacts, but durable trusted artifacts require
  explicit human approval.
- Resync stays delta-only. Changed resources write replacement vectors and graph
  facts before old chunk ids or stale graph facts are retired.
- Jira and Confluence are optional enrichment sources. Code/docs-only products
  must remain useful, but FalkorDB graph extraction still runs for every
  product.
- Trust beats cleverness. Material claims must be evidence-backed,
  confidence-scored, cited, and uncertainty-aware.

External references used in this design:

- FalkorDB: [official docs](https://docs.falkordb.com/),
  [GraphRAG SDK](https://docs.falkordb.com/genai-tools/graphrag-sdk.html),
  [indexing docs](https://docs.falkordb.com/cypher/indexing/),
  [constraints](https://docs.falkordb.com/commands/graph.constraint-create.html),
  [Python client](https://github.com/FalkorDB/falkordb-py).
- Qdrant: [text search and BM25 sparse vectors](https://qdrant.tech/documentation/search/text-search/),
  [hybrid queries and RRF](https://qdrant.tech/documentation/search/hybrid-queries/).
- GraphRAG: [Microsoft GraphRAG docs](https://microsoft.github.io/graphrag/),
  [GraphRAG paper](https://arxiv.org/abs/2404.16130).
- Jira: [webhooks](https://developer.atlassian.com/cloud/jira/platform/webhooks/),
  [REST API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/).

## Target Architecture

Nexus should use a three-store model:

- SQLite registry: source of truth for products, runtime sources, sync runs,
  source manifests, proposal state, council sessions, and graph extraction
  bookkeeping.
- Qdrant: derived semantic and lexical evidence index for code, docs, approved
  skills, optional Jira text, optional Confluence text, PR summaries, runbooks,
  and generated context evidence.
- Graph store: derived product-system topology index for services, modules,
  APIs, schemas, tests, owners, tickets, PRs, commits, incidents, runtime
  dependencies, and product flows.

FalkorDB is the first production graph backend because it is built for
low-latency property graph traversal, supports OpenCypher, range/full-text/vector
indexes, constraints, official clients, and multi-graph isolation. Operational
work must cover Redis/FalkorDB durability, HA decisions, licensing review, graph
noise, extraction cost, and keeping finalization delta-sized.

Alternatives are historical context, not active implementation targets:

- Qdrant + repo map only: lowest operational risk, enough for many coding
  context tasks, weak at transitive impact and ownership paths.
- Neo4j: already present as a dependency, mature Cypher ecosystem, but
  GraphRAG was previously cut from Nexus and needs a measured win before being
  reintroduced.
- Kuzu or another embedded graph: attractive for local single-node dev and low
  ops, weaker for multi-user enterprise deployments and remote product indexes.

The required direction is a graph adapter boundary. Internally, Nexus talks to
`GraphStore` concepts rather than FalkorDB-specific APIs. FalkorDB is the
backend from the start. Graph-powered answer quality remains eval-gated, but
the graph service is not optional and there is no graph-disabled mode.

## A. Ingestion And Data Pipeline Architecture

The current `run_ingest()` pipeline should remain the backbone:

```text
list resources -> read + hash -> manifest diff
  unchanged -> skip
  removed -> delete derived state -> delete manifest row
  added/updated -> chunk -> embed -> sparse encode -> Qdrant upsert
                -> graph extract -> graph upsert
                -> retire stale chunks/facts
                -> manifest update
```

Graph extraction must use the same delta classification as embedding. The source
manifest should add graph fields alongside existing embedding/enrichment fields:

- `graph_extraction_version`: hash of extractor code version, language detector
  version, schema version, and enabled source enrichers.
- `graph_status`: `pending`, `complete`, `partial`, `failed`, or empty.
- `graph_fact_ids_js`: stable ids of nodes/edges emitted from this resource.
- `graph_indexed_at`: timestamp for last successful graph write.

Repository parsing should start deterministic:

- Reuse tree-sitter parsing already used by `nexus/ingest/chunker.py` and
  `nexus/retrieval/repomap.py`.
- Extract `CodeFile`, `Module`, `Function`, `Class`, imports/exports, route
  handlers, test declarations, migrations, config files, feature flag reads, and
  schema definitions from syntax and file conventions.
- Detect services and UI apps from repo roots, package manifests, framework
  conventions, Docker/compose/Kubernetes files, app routers, API route files,
  and deploy configs.
- Extract database tables/events/config keys from migration files, ORM models,
  SQL, schema files, and config declarations.

Docs should remain Qdrant-indexed evidence and also become graph evidence:

- Markdown/MDX chunks keep heading-aware chunking and contextual summaries.
- Docs, ADRs, runbooks, and API docs create `Document`, `ADR`, and `Runbook`
  nodes.
- Deterministic links come from file paths, headings, explicit code symbols,
  route names, service names, ticket keys, and ADR ids.
- LLM extraction may propose extra `DOCUMENTS`, `CONSTRAINS`, `MENTIONS`, or
  `RELATED_TO` edges, but these start lower confidence and must retain source
  citations.

Jira and Confluence should be optional source types:

- Jira supports webhook-driven sync where available and periodic polling as a
  fallback. Webhook deliveries must dedupe by `X-Atlassian-Webhook-Identifier`
  because Jira can retry deliveries.
- Jira REST paging and expansion should be handled explicitly so descriptions,
  comments, links, and status history do not require blind refetch loops.
- Confluence sync should ingest page text and metadata first. Visual diagrams,
  screenshots, and attachments are out of scope until Nexus has a visual
  document pipeline.

Failure handling must preserve good existing state:

- For added/updated resources, write replacement Qdrant chunks and graph facts
  before retiring stale chunk ids or graph facts.
- If embedding fails, keep old vectors and manifest state.
- If graph extraction fails after vector upsert, mark `graph_status="failed"`
  or `partial`, count the sync as partial/error, and keep old active graph facts.
- If graph write fails, do not update graph manifest fields, count the sync as
  partial/error, and retry next sync.
- Removed resources retire graph facts only after delete/retire succeeds; failed
  deletes keep manifest rows so cleanup retries.

## B. Entity Resolution And Relationship Extraction

Stable identity rules should favor deterministic ids over names:

- Product: `product:<product_id>`.
- Source: `source:<product_id>:<source_key>`.
- Repository: `repo:<product_id>:<provider>:<owner>/<repo>`.
- Code file: `file:<product_id>:<resource_uri>`.
- Symbol: `symbol:<product_id>:<resource_uri>:<qualified_name>:<kind>`.
- API endpoint: `api:<product_id>:<method>:<normalized_path>:<service_id>`.
- DB table: `dbtable:<product_id>:<schema_or_db>:<table>`.
- Jira ticket: `jira:<product_id>:<site>:<project_key>-<number>`.
- PR/commit: provider-native id scoped through product and repo.
- Owner/team: normalized identity plus product id, never org-wide tenancy.

Rename handling should use aliases, not destructive rewrites:

- Keep `canonical_name`, `aliases`, `first_seen`, `last_seen`, and `source_refs`.
- Treat file moves as new path facts linked by `RENAMED_FROM` only when git
  history or provider metadata proves the rename.
- Treat symbol renames as probable until commit/PR evidence or high-confidence
  AST similarity supports them.

Relationship extraction should be layered:

- Deterministic extraction: imports, declarations, route handlers, test coverage,
  schema/table declarations, migration writes, feature flag reads, package
  dependencies, ownership files, commit/PR file changes.
- Heuristic extraction: commit messages and branch names linking Jira keys;
  docs mentioning API paths, service names, ADR ids, or runbook titles.
- LLM extraction: ambiguous docs-to-feature or incident-to-root-cause links. LLM
  facts must have lower initial confidence, explicit evidence, and `status`
  separate from deterministic facts.

Conflict resolution should not pick one truth silently:

- Code beats docs for current implementation.
- ADRs beat code comments for intended constraints, but may be stale.
- Ownership files beat Jira assignees for durable ownership.
- Jira assignees/reporters are delivery metadata, not ownership.
- Runbooks and incidents can raise operational relevance but should not assert
  root cause without direct evidence.

Confidence should combine:

- Extraction method: deterministic > heuristic > LLM.
- Evidence count and diversity: code + docs + PR stronger than one mention.
- Freshness: recent active facts stronger than stale docs.
- Conflict penalty: disagreement lowers confidence and surfaces uncertainty.
- Human correction: approved corrections override automated extraction within the
  product scope.

## C. FalkorDB Graph Schema

Every node and edge must include:

- `product_id`
- `stable_id`
- `source_refs`
- `confidence`
- `extraction_method`
- `last_seen`
- `freshness`
- `status`: `active`, `stale`, `corrected`, or `deleted`

Essential nodes:

- `Product`
- `Source`
- `Repository`
- `Service`
- `UIApp`
- `UIScreen`
- `APIEndpoint`
- `Module`
- `CodeFile`
- `Function`
- `Class`
- `DBTable`
- `Migration`
- `EventTopic`
- `Config`
- `FeatureFlag`
- `Test`
- `Document`
- `ADR`
- `Runbook`
- `JiraTicket`
- `Epic`
- `PR`
- `Commit`
- `Incident`
- `Owner`
- `Actor`
- `Team`
- `ErrorSignature`
- `ProductFlow`

Essential edges:

- `CONTAINS`
- `DECLARES`
- `IMPORTS`
- `CALLS`
- `DEPENDS_ON`
- `HANDLES`
- `EXPOSES`
- `READS`
- `WRITES`
- `PRODUCES`
- `CONSUMES`
- `COVERS`
- `DOCUMENTS`
- `CONSTRAINS`
- `OWNS`
- `ASSIGNED_TO`
- `IMPLEMENTS`
- `RESOLVES`
- `CHANGED`
- `AFFECTS`
- `MENTIONS`
- `RELATED_TO`
- `PART_OF_FLOW`

Node groups:

- Code topology: `Repository`, `Module`, `CodeFile`, `Function`, `Class`,
  `Test`.
- Runtime topology: `Service`, `UIApp`, `UIScreen`, `APIEndpoint`, `DBTable`,
  `Migration`, `EventTopic`, `Config`, `FeatureFlag`.
- Knowledge artifacts: `Document`, `ADR`, `Runbook`.
- Delivery/history: `JiraTicket`, `Epic`, `PR`, `Commit`.
- Operations: `Incident`, `ErrorSignature`.
- Responsibility/product: `Product`, `Source`, `Owner`, `Team`, `ProductFlow`.

Recommended indexes/constraints:

- Unique constraint on `(product_id, stable_id)` for every node label where
  FalkorDB supports the needed pattern.
- Range indexes for `product_id`, `stable_id`, `status`, and high-traffic lookup
  fields like route path, file URI, Jira key, commit SHA, and owner name.
- Relationship indexes for `product_id`, `status`, and `last_seen` where useful.
- Full-text indexes only for graph-side entity lookup, not as replacement for
  Qdrant retrieval.
- Vector indexes in FalkorDB should be deferred. Qdrant already owns vector and
  sparse retrieval; duplicating embeddings in the graph adds cost unless evals
  prove a benefit.

Tenancy model:

- Prefer one FalkorDB graph per product if operationally feasible; graph name is
  derived from product id and sanitized.
- Also store `product_id` on all nodes/edges and filter all generated Cypher by
  product id. This gives defense in depth and supports migration to shared graph
  layouts if needed.
- Never traverse from one product graph to another during agent query handling.

## D. Qdrant Vector Schema

Keep the current two-collection strategy unless evals show a better layout:

- `nexus_code`: code chunks with dense vector and BM25 sparse vector.
- `nexus_text`: docs, skills, tickets, PR summaries, runbooks, incidents, and
  natural-language chunks with dense vector and BM25 sparse vector.

Current payload fields remain mandatory:

- `product_id`
- `resource_uri`
- `source_id`
- `source_key`
- `content_hash`
- `embedding_version`
- `indexed_at`
- `mime`
- `kind`
- `start_line`
- `end_line`
- `context_path`
- `content`

Proposed payload additions:

- `graph_node_ids`: graph entities directly represented by the chunk.
- `entity_ids`: normalized aliases/entities mentioned in the chunk.
- `source_ref`: compact citation object or key, stable across vector/graph.
- `citation_anchor`: human-readable file/page/ticket anchor.
- `graph_extraction_version`: graph extractor version used when links were
  attached.
- `artifact_type`: `code`, `doc`, `skill`, `jira`, `pr`, `runbook`, `incident`.

Embedding strategy:

- Code chunks continue using code-aware query/passages through the configured
  embedder. Optional HQE stays off by default but remains available.
- Docs and natural language chunks continue heading/contextual retrieval
  prefixes where enabled.
- Jira/PR/runbook chunks use text collection and should include product,
  ticket/PR status, linked repos, and date in the chunk context.
- Sparse BM25 remains valuable for symbols, route paths, error signatures,
  ticket keys, and exact owner names.
- Reranker remains the final ordering step. Graph results should seed or filter
  candidates, not bypass rerank when text evidence is returned.

Graph ids in Qdrant should be used as:

- Filters for known entity queries.
- Boost signals when graph traversal identifies likely relevant entities.
- Citation joins from generated answers back to graph facts and source chunks.
- Debug metadata so a retrieved chunk can explain why it was surfaced.

## E. Query And GraphRAG Orchestration

Nexus should answer arbitrary product questions through one generic GraphRAG
engine. The system must not depend on hardcoded feature routes to answer
multi-hop questions. Named intents such as context lookup, change impact,
dependency trace, ownership discovery, similar work, observability, and effort
estimation are eval presets, UI labels, or prompt-shaping hints only.

Do not start with an LLM classifier. Use deterministic seed extraction from the
current file, symbol-like tokens, route paths, ticket keys, code paths, and query
wording. If multiple graph entities match, return a clarification with concrete
entity options.

Use Qdrant first, then FalkorDB when:

- The query is vague and needs semantic search to find seed entities.
- Natural language mentions a feature concept not present as a graph canonical
  name.
- Similar-work discovery starts from text, PR summaries, or docs.

Use FalkorDB first, then Qdrant when:

- The query names a known file, symbol, route, Jira key, service, or error
  signature.
- The question needs bounded multi-hop context.
- The graph can produce candidate entity ids that Qdrant then expands into
  cited evidence.

Traversal controls:

- Default max depth 2 for coding context, 3 for impact/dependency, 1 for
  ownership unless explicitly expanded.
- Fanout caps per edge type; high-degree edges like `MENTIONS` need stricter
  caps than `DECLARES` or `COVERS`.
- Prefer active, fresh, deterministic edges.
- Penalize stale, corrected, deleted, low-confidence, and LLM-only facts.
- Stop traversal when evidence budget is full or marginal confidence drops below
  route threshold.

Answer policy:

- Every material claim cites Qdrant chunks, source refs, or graph facts with
  source refs.
- Structural graph claims include confidence and path summary.
- Unsupported root-cause claims are forbidden; present hypotheses as hypotheses.
- Unknowns are first-class output, not hidden.

Graph-node filtered retrieval:

- Every Qdrant chunk stores direct `graph_node_ids`, `entity_ids`, `source_ref`,
  `citation_anchor`, `artifact_type`, and `graph_extraction_version`.
- Resolved and traversed graph node ids become product-scoped Qdrant filters or
  boost signals.
- Graph-filtered hits are merged with normal dense + BM25 hits through RRF, then
  reranked.
- Final answers synthesize only from cited Qdrant evidence plus source-backed
  graph facts.

## F. Example: Transitive Impact Analysis

Scenario: a developer modifies `github:acme/platform/shared/auth/token_policy.py`,
a shared module used by multiple services.

1. Target resolution
   - Resolve current file to `CodeFile`.
   - Resolve declared symbols to `Function`/`Class`.
   - Use import graph and Qdrant symbol search to validate target.
   - If multiple files or repos match, return `CLARIFY`.

2. FalkorDB traversal
   - Start at `CodeFile` and contained symbols.
   - Traverse incoming `IMPORTS` and `CALLS` to dependent modules.
   - Traverse `CONTAINS` upward to repositories/services.
   - Traverse `HANDLES`/`EXPOSES` to APIs.
   - Traverse `READS`/`WRITES` for DB tables and configs.
   - Traverse `COVERS` to tests.
   - Traverse `DOCUMENTS`/`CONSTRAINS` to docs and ADRs.
   - Traverse `CHANGED`/`IMPLEMENTS`/`RESOLVES` to PRs and Jira tickets.

3. Affected set
   - Services: auth API, billing API, internal worker.
   - APIs: token refresh route, session validation endpoint.
   - Tests: unit tests for token expiry, integration tests for refresh flow.
   - Docs: auth runbook, ADR on token rotation.
   - Jira/PRs: optional if source configured; otherwise omitted with note.

4. Qdrant evidence retrieval
   - Query code collection with target symbols and affected route names.
   - Query text collection for ADR/runbook constraints.
   - Filter or boost by graph entity ids from traversal.
   - Retrieve similar PR/Jira text only if optional sources exist.

5. Reranking
   - Rerank passages against impact question.
   - Keep top evidence across code, tests, docs, and tickets.
   - Drop graph-only claims that cannot be backed by source refs.

6. Final impact brief
   - Summary: likely affected services and APIs.
   - Required checks: tests to run, docs to update, owners to notify.
   - Risk: token semantics shared by billing and worker service.
   - Citations: file line anchors, ADR/runbook anchors, ticket/PR ids if present.

7. Confidence and unknowns
   - High confidence: deterministic import and route handler edges.
   - Medium confidence: docs mentioning the module but not exact symbol.
   - Low confidence: LLM-extracted feature relationship.
   - Unknown: runtime traffic or service mesh dependency if observability source
     is not configured.

8. Suggested next checks
   - Run tests covering affected symbols.
   - Inspect recent PRs touching same module.
   - Ask owner/team if ownership confidence is low.
   - Confirm runtime dependency with observability data once available.

## G. Scalability, Sync, And Latency

Sync strategy:

- GitHub source sync remains source-driven and delta-safe.
- Webhooks should enqueue source sync requests, not run extraction inline.
- Debounce noisy events per `(product_id, source_key)` and coalesce file changes.
- Batch graph writes per resource batch, matching current ingest batching.
- Background enrichment and graph extraction should be independently retryable.

Consistency:

- SQLite manifest is the truth for last successful vector and graph versions.
- Qdrant and graph store are derived indexes.
- Write replacement facts first; retire stale facts second; update manifest last.
- Store sync run status as `done`, `partial`, or `error` with separate vector and
  graph counts.

Caching:

- Entity resolution cache keyed by product id + normalized mention.
- Common traversal cache keyed by product id + route + seed ids + graph version.
- Context pack cache keyed by product id + route + seed ids + evidence version.
- Cache invalidation uses changed resource URIs, graph fact ids, and product
  graph version.

Latency targets for agent thought loops:

- `CODE_AGENT_CONTEXT`: p50 under 800 ms after warm handles; p95 under 2 s.
- `CONTEXT_LOOKUP`: p50 under 1.5 s; p95 under 4 s.
- `CHANGE_IMPACT`: p50 under 3 s; p95 under 8 s for bounded graph traversal plus
  rerank.
- `EFFORT_ESTIMATION` and `PRODUCT_RESEARCH`: allowed slower, but should stream
  partial results if LLM synthesis is involved.

Failure behavior:

- If FalkorDB is unavailable, startup, sync, health, and GraphRAG calls fail
  clearly; Nexus does not silently switch to graph-disabled behavior.
- If Qdrant is unavailable, final cited answers cannot be produced; topology
  alone is insufficient for material claims.
- If reranker fails, keep current soft-fail behavior and return fused order with
  lower confidence.
- If optional Jira/Confluence is unavailable, omit those enrichment signals and
  say so.

## H. Trust, Governance, And Human Correction

Evidence requirements:

- Code implementation claims require code citations.
- Architecture/constraint claims require ADR/doc/code citations.
- Ownership claims require ownership file, repo metadata, or explicit human
  correction; Jira assignee alone is not durable ownership.
- Jira assignees/reporters are delivery `Actor` facts, not `Owner` facts.
- Root-cause claims require incident/runbook/log evidence; otherwise they are
  hypotheses.

Correction workflow:

- Users can mark graph edges as wrong, stale, missing, or ambiguous.
- Corrections create product-scoped correction records in SQLite.
- Corrected facts keep previous source refs for audit and set automated facts to
  `corrected` rather than deleting them silently.
- Human-approved corrections override automated facts during retrieval.
- Agents may propose corrections, but approval is required before a trusted edge
  or durable artifact changes status.

Governance:

- Every graph query includes product scope.
- Connector credentials remain source-scoped and Fernet-encrypted.
- Audit logs track source sync, graph extraction version, correction actor,
  approval actor, and served context ids.
- Derived graph facts can be deleted with product deletion alongside Qdrant
  chunks, repo maps, proposals, sessions, and skills.

Agent presentation rules:

- Include confidence and unknowns for impact/effort answers.
- Separate "known from evidence" from "likely because graph path suggests".
- Never hide stale or conflicting evidence.
- Never turn graph connectivity into causality without evidence.

## I. Evaluation Strategy

Graph extraction correctness:

- Build golden fixtures from small repos with expected nodes/edges.
- Gate: deterministic extractor precision >= 0.95 and recall >= 0.85 for code
  topology edges before enabling graph-backed coding context.

Entity resolution accuracy:

- Golden aliases for files, symbols, services, routes, ticket keys, and owners.
- Gate: top-1 resolution >= 0.90 for exact identifiers; >= 0.80 for natural
  language mentions; ambiguous cases must return `CLARIFY`.

Traversal relevance:

- Golden impact/dependency questions with expected affected files/services/tests.
- Gate: graph traversal recall@20 improves over Qdrant-only seed retrieval by at
  least 10 percent relative without more than 5 percent precision loss.

Current retrieval vs graph-expanded retrieval:

- Reuse `tests/eval/queries.json` and add graph-focused evals.
- Gate: no regression below current recall@10/MRR floors.
- Graph expansion default requires statistically meaningful improvement on
  multi-hop tasks, not only neutral performance on lookup tasks.

Generic GraphRAG quality:

- Golden arbitrary product questions must include single-hop, multi-hop,
  ambiguous, broad natural language, and no-answer cases.
- Gate: citation faithfulness >= 0.90.
- Gate: unsupported material claim rate stays near zero.
- Gate: graph-node filtered retrieval improves multi-hop recall@20 over
  Qdrant-only without unacceptable precision loss.
- Gate: latency is tracked for entity resolution, traversal, graph-filtered
  retrieval, rerank, and synthesis.

Impact analysis accuracy:

- Use merged PRs as historical ground truth: changed shared files -> actual
  downstream files/tests/docs touched in same PR or follow-up PRs.
- Gate: affected-test/service recall@10 >= 0.75 and false positive review burden
  acceptable in manual review.

Effort analysis usefulness:

- Compare estimated complexity bands against historical PR size, touched files,
  review duration, and linked ticket cycle time.
- Gate: human reviewers rate estimates useful >= 4/5 on at least 70 percent of
  sampled cases before exposing as first-class MCP route.

Onboarding/context-pack quality:

- Evaluate whether graph-backed context helps answer "where is X implemented",
  "what owns Y", and "how does flow Z work" with cited sources.
- Gate: citation faithfulness >= 0.90 and answer relevance >= 0.85.

Hallucination and citation faithfulness:

- LLM answers judged against retrieved evidence.
- Gate: unsupported material claim rate <= 5 percent for graph routes.
- Any route that returns unsupported root cause claims fails.

Latency and cost:

- Track p50/p95 latency for entity resolution, graph traversal, Qdrant retrieval,
  rerank, synthesis, and total route time.
- Gate: graph-backed coding context p95 <= 2 s; impact p95 <= 8 s on target
  product sizes before default MCP enablement.

## J. Recommended Phased Roadmap

Phase 1: deterministic code/spec/test graph extraction

- Add graph extraction versioning and require FalkorDB-backed writes.
- Extract repositories, files, modules, symbols, imports, APIs, tests, configs,
  migrations, and docs links.
- Keep graph answering quality eval-gated, but do not support a graph-disabled
  runtime mode.
- Build golden extractor and resolver evals first.

Phase 2: generic graph-filtered GraphRAG

- Resolve arbitrary product questions to graph entities.
- Traverse bounded multi-hop neighborhoods.
- Filter/boost Qdrant evidence by graph node ids, merge with hybrid retrieval,
  and rerank.
- Expose one generic product agent endpoint and UI surface.

Phase 3: eval presets and product-agent quality gates

- Treat change impact, dependency trace, ownership, similar work, and debugging
  as eval presets over generic GraphRAG.
- Require multi-hop recall, citation faithfulness, unsupported-claim, and
  latency gates before raising default confidence.

Phase 4: optional Jira enrichment

- Add Jira source with REST sync first, webhooks second.
- Link tickets to commits, PRs, services, delivery actors, and files through
  keys and provider metadata.
- Do not require Jira for impact analysis.

Phase 5: optional Confluence enrichment

- Ingest text pages and metadata.
- Link docs/specs/runbooks to services/APIs/features.
- Defer visual diagram understanding.

Phase 6: effort estimation

- Use graph impact size, similar PRs/tickets, test coverage, ownership, and
  uncertainty.
- Present as estimate bands with evidence, not deterministic predictions.

Phase 7: cross-service debugging and observability

- Add incidents, error signatures, runbooks, and service ownership.
- Route `OBSERVABILITY_INVESTIGATION` through graph-first lookup plus Qdrant
  evidence.

Phase 8: optimization hotspot discovery

- Combine churn, sparse test coverage, incidents, docs gaps, dependency fanout,
  and stale ownership.
- Keep as proposal/recommendation flow with human approval for durable changes.

## Risks And Open Questions

- FalkorDB licensing and deployment model need enterprise review because it is a
  required service.
- Redis/FalkorDB persistence, backup, replication, and HA must be designed
  before production adoption.
- Graph noise can be worse than missing graph data if low-confidence LLM edges
  are treated as truth.
- Full-graph dedup/finalize steps can undermine delta-only sync if used
  carelessly.
- Product-per-graph isolation is clean but may complicate fleet operations at
  high product counts.
- Existing `neo4j` dependency should be explained or removed in a separate
  dependency cleanup.
- UI correction workflows need product decisions before implementation.
- Observability sources are not defined today; incident/error schema should wait
  until real source integrations exist.

## Final Recommendation

Proceed with required FalkorDB-backed, product-scoped GraphRAG without a
graph-off runtime path. Build deterministic product graph extraction for
code/spec/test/docs topology, store graph version/status in the SQLite manifest,
and keep Qdrant as the required citation/evidence retrieval layer. Graph
traversal should resolve and expand entities, graph-node filters should constrain
or boost Qdrant evidence, and reranking should remain the final ordering step.

The right first user-visible win is the product-scoped conversational agent over
generic GraphRAG. Jira, Confluence, effort estimation, cross-service debugging,
and hotspot discovery should follow as additional graph sources and eval presets
after the code/docs graph proves trustworthy.
