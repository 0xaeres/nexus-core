const curriculum = window.ROADMAP || [];
const capstones = window.CAPSTONES || [];

const phaseGuides = {
  1: {
    why: "Python is the operating language of modern AI engineering. The goal is not to become a language theorist; it is to become fluent enough that your attention can stay on latency, correctness, schemas, retries, and product behavior.",
    lab: "Build a FastAPI service with one `/chat` route, one `/health` route, request and response models, structured logs, and three fake model providers called concurrently with `asyncio.gather`.",
    mistakes: ["Learning syntax without building services.", "Treating type hints as decoration instead of contracts.", "Blocking the event loop with synchronous I/O inside async code."],
    resources: [["Python tutorial", "https://docs.python.org/3/tutorial/"], ["FastAPI docs", "https://fastapi.tiangolo.com/"], ["Pydantic docs", "https://docs.pydantic.dev/"], ["SQLAlchemy tutorial", "https://docs.sqlalchemy.org/en/20/tutorial/"]]
  },
  2: {
    why: "LLM engineering starts with a sober mental model. A model is not a database, a person, or a search engine. It is a conditional text generator with a context window, sampling behavior, strengths, and failure modes.",
    lab: "Run the same prompt ten times at different temperatures, compare outputs, then write a short model-selection memo for summarization, coding, extraction, and agent planning.",
    mistakes: ["Expecting the model to know private or recent facts.", "Using leaderboard rank as a product decision.", "Ignoring context-window position and truncation."],
    resources: [["The Illustrated Transformer", "https://jalammar.github.io/illustrated-transformer/"], ["Attention Is All You Need", "https://arxiv.org/abs/1706.03762"], ["SWE-bench", "https://www.swebench.com/"], ["Chatbot Arena", "https://lmarena.ai/"]]
  },
  3: {
    why: "Prompting becomes engineering when prompts are versioned, evaluated, structured, and connected to APIs. The craft is not finding magic words; it is designing reliable interfaces between user intent, context, tools, and model output.",
    lab: "Take one flaky extraction prompt, add a schema, add examples, add a regression file with twenty inputs, and measure exact JSON validity plus field-level accuracy.",
    mistakes: ["Writing negative-only instructions.", "Skipping examples for ambiguous formats.", "Shipping prompts that are not in version control or a managed prompt registry."],
    resources: [["OpenAI API docs", "https://platform.openai.com/docs"], ["Anthropic docs", "https://docs.anthropic.com/"], ["DSPy docs", "https://dspy.ai/"], ["Prompt caching overview", "https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching"]]
  },
  4: {
    why: "RAG is how models become useful on private, changing, or source-bound knowledge. The hard parts are ingestion fidelity, chunk quality, retrieval evaluation, and citation discipline.",
    lab: "Create a small RAG system over twenty PDFs or markdown files: parse, chunk, embed, index, retrieve, rerank, answer with citations, and score retrieval on a golden question set.",
    mistakes: ["Using fixed-size chunks for structured documents.", "Judging answer quality without checking retrieved context.", "Deleting or replacing indexes without a reproducible ingestion manifest."],
    resources: [["RAGAS", "https://docs.ragas.io/"], ["Qdrant docs", "https://qdrant.tech/documentation/"], ["pgvector", "https://github.com/pgvector/pgvector"], ["Docling", "https://ds4sd.github.io/docling/"]]
  },
  5: {
    why: "Tools turn an LLM from a text generator into an actor. This power has to be shaped with narrow schemas, clear descriptions, validation, timeouts, and human approval for risky operations.",
    lab: "Build a single research agent with three tools: search docs, read file, and create draft. Add structured tool returns, retries, timeouts, and a human approval gate before any write.",
    mistakes: ["Making tools too broad.", "Returning prose where structured data is needed.", "Letting tool output override system instructions."],
    resources: [["Model Context Protocol", "https://modelcontextprotocol.io/"], ["LangChain agents", "https://docs.langchain.com/oss/python/langchain/agents"], ["JSON Schema", "https://json-schema.org/learn/getting-started-step-by-step"], ["Playwright", "https://playwright.dev/"]]
  },
  6: {
    why: "Context engineering is the discipline of deciding what the model sees, where it appears, how long it stays, and when it is compressed or forgotten. Most agent failures are context failures wearing a different mask.",
    lab: "Implement a chat service with system/context/user separation, sliding-window history, retrieval snippets, summarized old turns, and a visible token budget report.",
    mistakes: ["Dumping everything into one prompt.", "Summarizing away facts needed for later tool calls.", "Persisting memory without privacy and deletion flows."],
    resources: [["LangGraph memory", "https://langchain-ai.github.io/langgraph/concepts/memory/"], ["Zep", "https://www.getzep.com/"], ["mem0", "https://mem0.ai/"], ["FAISS", "https://faiss.ai/"]]
  },
  7: {
    why: "Multi-agent systems are useful when different steps need different prompts, tools, permissions, or review criteria. They are expensive confusion machines when used just because the architecture sounds advanced.",
    lab: "Build a natural-language-to-SQL graph with planner, writer, validator, executor, and explainer nodes. Add typed state, retries, read-only execution, row caps, and traces.",
    mistakes: ["Using multi-agent orchestration for a simple tool call.", "Letting agents talk without shared state contracts.", "Creating loops without termination budgets."],
    resources: [["LangGraph docs", "https://langchain-ai.github.io/langgraph/"], ["AutoGen", "https://microsoft.github.io/autogen/"], ["CrewAI", "https://docs.crewai.com/"], ["Pydantic AI", "https://ai.pydantic.dev/"]]
  },
  8: {
    why: "Production AI needs guardrails and observability because model output is probabilistic, tool calls have side effects, and failures can be expensive or harmful. Good systems are measurable before they are clever.",
    lab: "Wrap an agent endpoint with input checks, output faithfulness checks, tool-call limits, trace logging, cost metrics, and a golden dataset regression command.",
    mistakes: ["Asking an LLM to enforce every safety rule.", "Only logging final answers.", "Changing prompts without running regression tests."],
    resources: [["LangSmith", "https://docs.smith.langchain.com/"], ["Langfuse", "https://langfuse.com/docs"], ["MLflow LLM evaluation", "https://mlflow.org/docs/latest/llms/llm-evaluate/"], ["AWS Bedrock Guardrails", "https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html"]]
  },
  9: {
    why: "Deployment is where AI engineering becomes software engineering again. You need storage, networking, secrets, containers, observability, cost controls, and release discipline.",
    lab: "Dockerize your FastAPI agent, deploy it behind a managed endpoint, store secrets outside the image, stream responses to a client, and load-test concurrency plus model rate limits.",
    mistakes: ["Putting provider keys in environment files committed to Git.", "Load-testing only the web server and not the model/tool bottlenecks.", "Treating cost as a finance problem instead of an engineering signal."],
    resources: [["AWS ECS", "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/Welcome.html"], ["AWS Lambda", "https://docs.aws.amazon.com/lambda/latest/dg/welcome.html"], ["AWS Secrets Manager", "https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html"], ["Server-sent events", "https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events"]]
  }
};

const topicExplanations = [
  [/async|await|gather|event loop|create_task|timeout/i, "Async code matters because LLM calls, database calls, and retrieval calls spend most of their time waiting. Use coroutines for I/O, put deadlines around every external request, and gather independent work only when the result order and failure policy are clear."],
  [/pydantic|schema|json schema|structured/i, "Schemas are the boundary between model-shaped text and application-shaped data. They make requests explicit, tool contracts inspectable, validation automatic, and failures easier to explain."],
  [/retrieval|rag|embedding|vector|bm25|rerank|chunk/i, "Retrieval quality is a pipeline property. Better answers come from preserving document structure, enriching chunks with metadata, combining lexical and semantic signals, and measuring whether the expected evidence appears in the top results."],
  [/guardrail|security|injection|pii|read-only|approval|sandbox/i, "Safety controls should be boring and close to the action. Validate inputs before the model, constrain tools while they run, and check outputs before users or downstream systems rely on them."],
  [/eval|benchmark|golden|precision|recall|faithfulness|regression/i, "Evaluation converts taste into engineering feedback. Keep a small golden set, track deterministic retrieval metrics where possible, and use LLM judges only for judgments that deterministic code cannot make."],
  [/agent|react|tool|mcp|computer use/i, "Agents work by alternating between model decisions and environment observations. Their reliability comes from narrow tools, typed state, bounded loops, clear permissions, and traces you can inspect after failure."],
  [/memory|context|compression|cache|history|token/i, "Memory is not one thing. Separate working context, session history, semantic cache entries, user preferences, and long-term facts so each can have its own budget, retention policy, and deletion story."],
  [/docker|ecs|lambda|s3|rds|dynamodb|gateway|secrets|deployment|cloud/i, "Cloud deployment turns prototypes into services. The core pattern is container or function compute, managed storage, least-privilege access, externalized secrets, logs, metrics, and a repeatable promotion path."],
  [/http|api|fastapi|requests|streaming/i, "APIs are the surface area of production AI. Treat status codes, retries, authentication, streaming, rate limits, and request models as first-class design choices rather than wiring details."],
  [/sql|database|postgres|cypher|neo4j/i, "Databases are both knowledge sources and risk surfaces. Use least privilege, parameterized queries, query limits, connection pools, and explicit transaction boundaries."],
  [/prompt|few-shot|zero-shot|chain|self/i, "Prompt techniques are experiments, not spells. Start with task, context, constraints, and output shape; add examples when the desired behavior is ambiguous; then measure the result on real cases."],
  [/model|llm|temperature|token|transformer|reasoning/i, "Model behavior is probabilistic and context-dependent. The same model can be excellent or poor depending on task framing, sampling settings, context placement, and whether it has the right external evidence."]
];

const chapterGuides = {
  1: {
    opening: "AI systems are ordinary software systems with unusual dependencies: slow remote models, probabilistic output, large payloads, typed tool contracts, and heavy I/O. Python is the language most of this ecosystem standardizes on, so the first concern is building correct, observable services rather than memorizing syntax trivia.",
    mechanics: "The foundation is a chain of contracts. Type hints document intent, Pydantic validates data at process boundaries, FastAPI exposes those contracts over HTTP, database sessions persist durable state, and async functions keep model calls from blocking each other.",
    diagram: `request JSON\n    |\n    v\nPydantic model -> business function -> model/database/API clients\n    |                                      |\n    v                                      v\nresponse model <---------------------- structured result`,
    failureModes: ["Blocking I/O inside an async route stalls unrelated users.", "Unvalidated dictionaries drift until a model or tool receives the wrong shape.", "Database connections leak when sessions are not scoped to requests."]
  },
  2: {
    opening: "A language model is a conditional probability engine over tokens. It receives a sequence of tokens, repeatedly predicts a distribution for the next token, samples or selects one token, and appends it to the context. Everything that feels conversational emerges from this loop.",
    mechanics: "The model's useful behavior depends on four things: the training distribution, the tokens currently visible in the context window, the decoding parameters used at generation time, and any external tools or retrieved evidence supplied by the application.",
    diagram: `text -> tokenizer -> token IDs -> transformer layers -> next-token distribution\n                                                       |\n                                                       v\n                                    sampling parameters choose the next token`,
    failureModes: ["The model can state false facts fluently because probability is not truth.", "Long contexts can bury important instructions or evidence.", "Benchmarks can overstate performance when the task distribution differs from your product."]
  },
  3: {
    opening: "Prompt engineering is interface design. A prompt defines the task, constraints, context, examples, and output contract the model must follow. In production, the prompt is code: versioned, tested, measured, and changed deliberately.",
    mechanics: "Reliable prompts separate instructions from data, specify the output format, include examples for ambiguous cases, and pair generation with validation. The API is the control surface because it exposes roles, streaming, tools, schemas, caching, and reproducible configuration.",
    diagram: `system instructions\n        +\nretrieved context\n        +\nuser task\n        +\noutput schema\n        v\nmodel response -> parser -> validator -> application`,
    failureModes: ["A prompt that works in a chat UI may fail through an API because hidden system instructions differ.", "Free-form outputs create brittle parsers.", "Long static prompts become expensive unless cached or shortened."]
  },
  4: {
    opening: "Retrieval-augmented generation gives a model access to information outside its weights. The application retrieves relevant evidence, places it in context, and asks the model to answer from that evidence. The generation step is only as trustworthy as the ingestion and retrieval steps before it.",
    mechanics: "A RAG system parses source documents, chunks them, enriches chunks with metadata, embeds searchable text, stores vectors and lexical indexes, retrieves candidates, reranks them, and generates an answer with citations. Evaluation measures both retrieved evidence and final answer quality.",
    diagram: `documents -> parse -> chunk -> enrich -> embed -> index\n                                                |\nquestion -> rewrite/filter -> retrieve -> rerank -> answer with citations`,
    failureModes: ["Poor chunking separates definitions from the terms they define.", "Vector-only search misses exact identifiers, dates, and error messages.", "Answer evaluation without retrieval evaluation hides the real defect."]
  },
  5: {
    opening: "Tools let a model interact with systems it cannot internally access: databases, files, APIs, browsers, calculators, and internal services. The model chooses a tool call, the application executes it, and the result becomes the next observation.",
    mechanics: "Good tools are narrow, typed, documented, bounded by timeouts, and safe by construction. MCP generalizes the tool boundary so many clients can discover and call tools exposed by many servers.",
    diagram: `user request -> model decides\n                    |\n                    v\ntool schema -> tool call -> application executes -> observation -> model continues`,
    failureModes: ["Broad tools make bad actions easy to request.", "Tool output can contain prompt injection and must be treated as untrusted data.", "Sensitive tools need human approval rather than polite instructions."]
  },
  6: {
    opening: "Context is the model's working memory. It is not unlimited, and it is not automatically organized. Context engineering is the practice of deciding what the model sees, where it appears, how much budget it gets, and when information is summarized, cached, retrieved, or forgotten.",
    mechanics: "A robust context layout separates system instructions, developer policy, retrieved context, memory, tool observations, and the current user request. Different memory types have different lifetimes: session turns, semantic cache entries, episodic memories, and long-term profile facts.",
    diagram: `SYSTEM: rules and role\nCONTEXT: retrieved evidence and memory\nHISTORY: recent user/assistant turns\nUSER: current request\nTOOLS: observations from executed actions`,
    failureModes: ["A summary can erase a constraint that mattered later.", "Long-term memory can become a privacy liability.", "Putting untrusted retrieved text near instructions increases injection risk."]
  },
  7: {
    opening: "A multi-agent system is a controlled decomposition of work across specialized model calls. Each agent can have different instructions, tools, state access, and acceptance criteria. The benefit is specialization; the cost is coordination complexity.",
    mechanics: "Most orchestrations are graphs: nodes perform work, edges route state, reducers merge parallel output, and checkpoints persist progress. Deterministic code should route whenever possible; model calls should be reserved for judgment, generation, or interpretation.",
    diagram: `planner\n  |\n  v\nworker A ----\\\nworker B -----> synthesizer -> validator -> final answer\nworker C ----/`,
    failureModes: ["Agents can talk past each other when state contracts are vague.", "Reflection loops can run forever without budgets.", "A single tool-using agent is often simpler and more reliable."]
  },
  8: {
    opening: "Guardrails and LLMOps make AI systems operable. Guardrails constrain inputs, outputs, and actions. LLMOps records what happened, measures quality, and catches regressions when prompts, models, data, or tools change.",
    mechanics: "Input checks should be deterministic and fast. Output checks can use rules, retrieval consistency, or model judges. Action checks live inside tools. Observability captures prompts, tokens, latency, cost, errors, tool calls, retrieved chunks, and evaluation scores.",
    diagram: `request -> input checks -> model/tool workflow -> output checks -> response\n              |                    |                  |\n              v                    v                  v\n           reject/log          traces/metrics       fallback`,
    failureModes: ["An LLM should not be the only thing enforcing hard safety rules.", "Final-answer logs are insufficient for debugging.", "Prompt changes without regression tests are production changes without tests."]
  },
  9: {
    opening: "Deployment turns an AI prototype into an AI service. The service needs durable storage, isolated compute, secret management, rate limits, streaming delivery, monitoring, and cost controls. The model provider is only one dependency in a larger distributed system.",
    mechanics: "A typical deployment uses object storage for documents, a relational database for state, a vector or search index for retrieval, containers or functions for compute, a gateway for traffic, and managed secrets for credentials. Capacity planning includes model rate limits and tool latency, not just CPU.",
    diagram: `browser/client -> API gateway -> container or function -> model provider\n                                  |             |          |\n                                  v             v          v\n                               database      object store  logs/metrics`,
    failureModes: ["Secrets in images or source control become incidents.", "Streaming over the wrong transport creates brittle chat UX.", "Concurrency fails at model and tool limits before the web framework fails."]
  }
};

const patternLessons = [
  {
    pattern: /variable|types|control flow|functions|\*args|decorator|comprehension|generator|type hint/i,
    explanation: "Python code is executed dynamically, but production AI services still need explicit contracts. Type hints make function intent inspectable by editors, test tools, Pydantic, and future maintainers. Generators and comprehensions keep transformations small and memory-conscious, while decorators are commonly used for retries, tracing, authorization, and caching.",
    mechanics: "Prefer pure functions for transformations, keep side effects at the boundary, and use type hints on every public function. Decorators should preserve metadata with `functools.wraps`, and generators should be used when the full result does not need to live in memory.",
    code: `from __future__ import annotations\n\nfrom collections.abc import Iterable\nfrom functools import wraps\nfrom time import perf_counter\n\n\ndef timed(fn):\n    @wraps(fn)\n    def wrapper(*args, **kwargs):\n        start = perf_counter()\n        try:\n            return fn(*args, **kwargs)\n        finally:\n            print({\"fn\": fn.__name__, \"ms\": (perf_counter() - start) * 1000})\n    return wrapper\n\n\n@timed\ndef normalize(lines: Iterable[str]) -> list[str]:\n    return [line.strip().lower() for line in lines if line.strip()]`
  },
  {
    pattern: /class|inheritance|encapsulation|polymorphism|dataclass|pydantic/i,
    explanation: "Object-oriented Python is most useful when it models stable concepts: requests, documents, tool results, model providers, repositories, and configuration. Dataclasses are lightweight containers for internal state. Pydantic models validate untrusted data crossing boundaries such as HTTP requests, tool calls, queues, and model output.",
    mechanics: "Use dataclasses for trusted in-memory objects and Pydantic for inputs and outputs. Keep inheritance shallow; protocols or small interfaces usually age better than deep base classes.",
    code: `from __future__ import annotations\n\nfrom dataclasses import dataclass\nfrom pydantic import BaseModel, Field\n\n\n@dataclass(frozen=True)\nclass Chunk:\n    id: str\n    text: str\n    source: str\n\n\nclass SearchRequest(BaseModel):\n    query: str = Field(min_length=3)\n    top_k: int = Field(default=5, ge=1, le=20)\n\n\nclass SearchResult(BaseModel):\n    chunk_id: str\n    score: float\n    text: str`
  },
  {
    pattern: /list|tuple|set|dict|namedtuple|defaultdict|counter|deque/i,
    explanation: "Data structures encode access patterns. Lists preserve order, tuples represent fixed records, sets answer membership questions, dictionaries map stable keys to values, `Counter` counts events, `defaultdict` groups without repeated existence checks, and `deque` gives efficient queue behavior.",
    mechanics: "Choose the structure that makes illegal operations awkward. For retrieval and agent systems, dictionaries commonly hold state by ID, sets deduplicate chunk IDs, Counters summarize tool failures, and deques implement bounded message windows.",
    code: `from collections import Counter, defaultdict, deque\n\nmessages = deque(maxlen=6)\nmessages.append({\"role\": \"user\", \"content\": \"Summarize the policy\"})\n\nchunks_by_source = defaultdict(list)\nfor chunk in retrieved_chunks:\n    chunks_by_source[chunk.source].append(chunk)\n\nfailure_counts = Counter(event.tool for event in trace if event.error)`
  },
  {
    pattern: /try|except|finally|exception|context manager|json|csv|binary|file/i,
    explanation: "Error handling defines how a service fails. AI applications call flaky networks, parse messy documents, and handle model output that may not match expectations. Exceptions should carry enough context to diagnose the failure without leaking secrets.",
    mechanics: "Catch errors at boundaries where recovery is possible. Use context managers for resources that must be closed. Keep parsing code explicit: load bytes, decode, validate, and convert into typed objects.",
    code: `from contextlib import contextmanager\nimport json\n\n\nclass InvalidModelOutput(ValueError):\n    pass\n\n\n@contextmanager\ndef open_json(path: str):\n    handle = open(path, encoding=\"utf-8\")\n    try:\n        yield json.load(handle)\n    finally:\n        handle.close()\n\n\ndef parse_tool_args(raw: str) -> dict:\n    try:\n        value = json.loads(raw)\n    except json.JSONDecodeError as exc:\n        raise InvalidModelOutput(\"tool arguments were not valid JSON\") from exc\n    if not isinstance(value, dict):\n        raise InvalidModelOutput(\"tool arguments must be an object\")\n    return value`
  },
  {
    pattern: /requests|http|verb|headers|status|authentication|rate limit|retry|backoff/i,
    explanation: "HTTP APIs are contracts over the network. Methods describe intent, status codes describe outcome, headers carry metadata and authentication, and retries must respect idempotency. AI services use HTTP for model providers, retrievers, internal tools, and user-facing endpoints.",
    mechanics: "Set timeouts on every request. Retry transient failures such as 429 and 503 with exponential backoff. Do not blindly retry non-idempotent writes unless the API supports idempotency keys.",
    code: `import requests\nfrom tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type\n\n\nclass TransientHTTPError(RuntimeError):\n    pass\n\n\n@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), retry=retry_if_exception_type(TransientHTTPError))\ndef get_json(url: str, token: str) -> dict:\n    response = requests.get(url, headers={\"Authorization\": f\"Bearer {token}\"}, timeout=10)\n    if response.status_code in {429, 500, 502, 503, 504}:\n        raise TransientHTTPError(response.text)\n    response.raise_for_status()\n    return response.json()`
  },
  {
    pattern: /psycopg|sqlalchemy|connection pooling|raw sql|postgres|rds|database/i,
    explanation: "Databases store durable state: users, conversations, document manifests, tool runs, evaluations, and job progress. ORMs reduce boilerplate, but SQL remains the language of constraints, indexes, joins, and performance.",
    mechanics: "Use connection pools because opening a database connection per request is expensive. Use transactions for multi-step writes, parameterized queries for safety, and raw SQL when you need database-specific features or carefully tuned performance.",
    code: `from sqlalchemy import create_engine, text\nfrom sqlalchemy.orm import sessionmaker\n\nengine = create_engine(DB_URL, pool_size=5, max_overflow=10, pool_pre_ping=True)\nSessionLocal = sessionmaker(bind=engine)\n\n\ndef mark_job_done(job_id: str) -> None:\n    with SessionLocal.begin() as session:\n        session.execute(\n            text(\"update ingest_jobs set status = :status where id = :id\"),\n            {\"status\": \"done\", \"id\": job_id},\n        )`
  },
  {
    pattern: /fastapi|uvicorn|openapi|dependency injection|endpoint/i,
    explanation: "FastAPI turns Python type contracts into HTTP contracts. Request models validate input, response models document output, dependencies inject shared resources such as database sessions or model clients, and OpenAPI documentation comes from the same definitions used at runtime.",
    mechanics: "Keep route handlers thin. Validate at the edge, call application services, and return typed responses. Avoid doing slow synchronous work directly in async routes.",
    code: `from fastapi import Depends, FastAPI\nfrom pydantic import BaseModel\n\napp = FastAPI()\n\nclass ChatRequest(BaseModel):\n    message: str\n\nclass ChatResponse(BaseModel):\n    answer: str\n    model: str\n\n\ndef get_model_client() -> ModelClient:\n    return ModelClient(timeout=20)\n\n\n@app.post(\"/chat\", response_model=ChatResponse)\nasync def chat(req: ChatRequest, client: ModelClient = Depends(get_model_client)):\n    answer = await client.complete(req.message)\n    return ChatResponse(answer=answer, model=client.name)`
  },
  {
    pattern: /asyncio|async\/await|gather|wait_for|create_task|event loop/i,
    explanation: "Asyncio is cooperative concurrency. A coroutine yields control while waiting for I/O, letting other coroutines run on the same thread. This is ideal for LLM calls, retrieval, database queries through async drivers, and telemetry writes.",
    mechanics: "`asyncio.gather` runs independent operations concurrently. `asyncio.wait_for` enforces a deadline. `create_task` schedules background work, but background tasks still need error handling and shutdown behavior.",
    code: `import asyncio\n\nasync def ask_all(prompt: str) -> dict[str, str]:\n    tasks = {\n        \"fast\": asyncio.create_task(fast_model(prompt)),\n        \"accurate\": asyncio.create_task(accurate_model(prompt)),\n        \"cheap\": asyncio.create_task(cheap_model(prompt)),\n    }\n    results = {}\n    for name, task in tasks.items():\n        try:\n            results[name] = await asyncio.wait_for(task, timeout=8)\n        except TimeoutError:\n            results[name] = \"timed out\"\n    return results`
  },
  {
    pattern: /trained|snapshot|knowledge cutoff|probabilistic|different outputs/i,
    explanation: "Model weights contain statistical patterns from training, not a live connection to truth. A knowledge cutoff means some facts are absent or stale. Probabilistic decoding means identical prompts can produce different completions unless decoding is made deterministic.",
    mechanics: "Treat the model as a reasoning and language component, not an authority. For private or recent facts, retrieve evidence. For deterministic workflows, lower randomness and validate outputs.",
    code: `# Same prompt, different sampling settings\nsettings = [\n    {\"temperature\": 0.0, \"top_p\": 1.0},\n    {\"temperature\": 0.7, \"top_p\": 0.9},\n]\n\nfor cfg in settings:\n    print(call_model(\"Name three risks of RAG\", **cfg))`
  },
  {
    pattern: /tokenization|context windows|sampling|temperature|top-p|top-k|transformer|attention|lost in the middle/i,
    explanation: "Models do not read characters; they read tokens. The context window is the maximum token sequence visible for one request. Attention lets the model condition on previous tokens, but long inputs dilute signal and can make middle content less influential.",
    mechanics: "Token budgeting is engineering work. Place critical instructions near stable positions, remove irrelevant context, and tune sampling based on the task: low randomness for extraction, higher randomness for brainstorming.",
    code: `def choose_temperature(task: str) -> float:\n    if task in {\"classification\", \"extraction\", \"tool_args\"}:\n        return 0.0\n    if task in {\"drafting\", \"ideation\"}:\n        return 0.7\n    return 0.2`
  },
  {
    pattern: /reasoning model|thinking tokens|reasoning effort|base model/i,
    explanation: "Reasoning models spend additional computation on intermediate reasoning before producing an answer. This can improve hard planning, math, code, and multi-step analysis, but it adds latency and cost.",
    mechanics: "Use a reasoning model when the task has real multi-step uncertainty and the answer value justifies the delay. Use a base model for extraction, rewriting, simple classification, and high-throughput chat.",
    code: `def route_model(task: Task) -> str:\n    if task.requires_planning or task.has_high_error_cost:\n        return \"reasoning-model\"\n    if task.is_simple_extraction:\n        return \"fast-base-model\"\n    return \"balanced-base-model\"`
  },
  {
    pattern: /benchmark|leaderboard|mmlu|gsm8k|humaneval|swe-bench|gpqa|mmmu|bfcl|arena/i,
    explanation: "Benchmarks are compressed signals about model behavior on particular datasets. They are useful for orientation, but they are not substitutes for an evaluation that resembles your own users, documents, tools, and failure costs.",
    mechanics: "Read what the benchmark measures, inspect contamination risk, compare confidence intervals if available, and build a micro-eval for your product workflow before switching models.",
    code: `def accuracy(rows):\n    correct = sum(row.expected == row.actual for row in rows)\n    return correct / len(rows)\n\n# A useful model eval row contains the input, expected output,\n# actual output, model config, and a reason for failure.`
  },
  {
    pattern: /openai sdk|anthropic sdk|message format|streaming|json mode|xml|system|assistant|prefill/i,
    explanation: "The API exposes the message stack explicitly. System messages set durable behavior, user messages carry the request, assistant messages can preserve prior outputs, and tool or structured-output options constrain what the model may emit.",
    mechanics: "Streaming sends partial tokens or events as they are produced. Structured output reduces parsing ambiguity but still needs validation because transport-level JSON validity is not the same as business correctness.",
    code: `from pydantic import BaseModel\n\nclass InvoiceFields(BaseModel):\n    vendor: str\n    invoice_number: str\n    total: float\n\nmessages = [\n    {\"role\": \"system\", \"content\": \"Extract invoice fields. Return only JSON.\"},\n    {\"role\": \"user\", \"content\": invoice_text},\n]\n\nraw = client.responses.create(model=\"model-name\", input=messages)\nfields = InvoiceFields.model_validate_json(raw.output_text)`
  },
  {
    pattern: /zero-shot|few-shot|costar|iterative|extraction|classification|transformation|generation|decomposition|chain of thought|self-consistency|self-refine|least-to-most|tree of thought|prompt/i,
    explanation: "Prompt patterns shape how the model maps input to output. Zero-shot relies on instruction only. Few-shot supplies examples. Decomposition breaks a hard task into easier steps. Self-refinement asks the model to critique and revise, which can help prose but can also reinforce wrong assumptions.",
    mechanics: "Use the simplest pattern that reaches the required reliability. For extraction and classification, examples and schemas usually beat elaborate reasoning. For planning, decomposition and explicit acceptance criteria are more valuable.",
    code: `SYSTEM = \"\"\"You classify support tickets.\nReturn JSON matching: {category: billing|bug|feature|other, priority: low|medium|high}.\nUse high priority only when the user is blocked or money is affected.\"\"\"\n\nEXAMPLE = \"\"\"Input: I was charged twice this month.\nOutput: {\"category\":\"billing\",\"priority\":\"high\"}\"\"\"`
  },
  {
    pattern: /versioning|a\/b|prompt caching|bedrock prompt|dspy|cost/i,
    explanation: "Prompts change system behavior and should be managed like code. Versioning gives rollback, experiments compare variants, and caching avoids repeatedly paying full price for stable long context such as policies, schemas, and tool manuals.",
    mechanics: "Store prompt identity, version, model, parameters, and evaluation result with each run. Use prompt optimization frameworks only after you have a real metric to optimize.",
    code: `PROMPT_VERSION = \"invoice-extract-v4\"\n\ntrace.metadata.update({\n    \"prompt_version\": PROMPT_VERSION,\n    \"model\": MODEL,\n    \"temperature\": 0,\n})`
  },
  {
    pattern: /embedding|cosine|dot product|euclidean|dimension/i,
    explanation: "An embedding maps text, images, or other data into a vector space where distance approximates semantic similarity. Similar vectors are retrieved as candidates for a query, but vector similarity is not proof of relevance.",
    mechanics: "Normalize vectors when using cosine similarity. Higher dimensions can preserve more information but increase storage and compute. Evaluate embedding models on your own queries and documents.",
    code: `import numpy as np\n\ndef cosine(a: list[float], b: list[float]) -> float:\n    av = np.array(a)\n    bv = np.array(b)\n    return float(np.dot(av, bv) / (np.linalg.norm(av) * np.linalg.norm(bv)))`
  },
  {
    pattern: /docling|layout|serialization|pymupdf|pdf|document ingestion/i,
    explanation: "Document ingestion converts messy files into structured information. Layout matters: a heading, table cell, code block, footer, and paragraph should not be treated as identical plain text.",
    mechanics: "The parser should preserve hierarchy and coordinates when possible. The serialized intermediate representation should be inspectable because every downstream retrieval error starts with the text you extracted.",
    code: `@dataclass\nclass Block:\n    kind: str          # heading, paragraph, table, code\n    text: str\n    page: int\n    path: tuple[str, ...]  # document hierarchy`
  },
  {
    pattern: /fixed-width|semantic chunk|overlap|parent-child|late chunk|chunk size/i,
    explanation: "Chunking defines the unit of retrieval. A chunk should be small enough to rank precisely and large enough to contain the information needed to answer. Structure-aware chunking usually beats arbitrary character windows.",
    mechanics: "Preserve section boundaries, add overlap only where continuity matters, keep parent references for expansion, and test chunk sizes with retrieval metrics instead of guessing.",
    code: `def chunk_by_heading(blocks, max_chars=1200):\n    current = []\n    size = 0\n    for block in blocks:\n        if block.kind == \"heading\" and current:\n            yield \"\\n\".join(current)\n            current, size = [], 0\n        if size + len(block.text) > max_chars and current:\n            yield \"\\n\".join(current)\n            current, size = [], 0\n        current.append(block.text)\n        size += len(block.text)\n    if current:\n        yield \"\\n\".join(current)`
  },
  {
    pattern: /pii|redaction|ner|key-phrase|metadata/i,
    explanation: "Chunk enrichment adds searchable and safety-relevant attributes. PII flags control exposure, entities support filtering and graph edges, key phrases help hybrid search, and metadata narrows retrieval to the right tenant, source, time, or document type.",
    mechanics: "Never rely on enrichment as a replacement for authorization. Metadata filters should be applied before generation and preferably during retrieval.",
    code: `metadata = {\n    \"product_id\": product_id,\n    \"source\": \"policy.pdf\",\n    \"page\": 12,\n    \"entities\": [\"refund\", \"subscription\"],\n    \"contains_pii\": False,\n}`
  },
  {
    pattern: /pinecone|weaviate|pgvector|chroma|s3 vector|opensearch|hnsw|ivf|faiss|qdrant/i,
    explanation: "Vector databases store embeddings and retrieve approximate nearest neighbors efficiently. The main tradeoffs are operational complexity, filtering support, latency, recall, cost, and whether the index belongs inside your existing database stack.",
    mechanics: "HNSW builds a navigable graph for fast approximate search. IVF clusters vectors and searches selected clusters. Both trade exact recall for speed and memory efficiency.",
    code: `# Conceptual vector search call\nresults = vector_index.search(\n    vector=query_embedding,\n    top_k=10,\n    filter={\"product_id\": product_id, \"contains_pii\": False},\n)`
  },
  {
    pattern: /hybrid|bm25|reranking|cross-encoder|metadata filtering|query expansion|colbert|colpali/i,
    explanation: "Hybrid retrieval combines dense semantic search with lexical matching. Dense search finds paraphrases; BM25 finds exact terms, identifiers, and rare words. Rerankers then score the best candidates with deeper query-document interaction.",
    mechanics: "Use reciprocal rank fusion or weighted score fusion to merge candidate lists. Apply metadata filters before retrieval when they represent hard constraints, not after generation.",
    code: `def rrf(rankings, k=60):\n    scores = {}\n    for ranking in rankings:\n        for rank, doc_id in enumerate(ranking, start=1):\n            scores[doc_id] = scores.get(doc_id, 0) + 1 / (k + rank)\n    return sorted(scores, key=scores.get, reverse=True)`
  },
  {
    pattern: /neo4j|cypher|graph|multi-hop|relationship/i,
    explanation: "Graphs represent explicit relationships: person works for company, drug treats condition, service depends on database, API calls endpoint. They help when the question asks for paths, constraints, or multi-hop relationships that vector similarity alone does not model.",
    mechanics: "Use graph retrieval when edge structure is central to the answer. Use vector retrieval when semantic text relevance is central. Many production systems use both.",
    code: `MATCH (drug:Drug {name: $drug})-[:TESTED_IN]->(trial)-[:TARGETS]->(condition:Condition)\nRETURN trial.id, condition.name\nLIMIT 20`
  },
  {
    pattern: /faithfulness|context relevance|answer relevance|precision@k|recall@k|f1|hit rate|mrr|ndcg|ragas|golden/i,
    explanation: "RAG evaluation separates retrieval quality from answer quality. Retrieval metrics ask whether the right evidence appeared. Answer metrics ask whether the generated response used evidence correctly and answered the question.",
    mechanics: "Precision@k measures how many retrieved chunks are relevant. Recall@k measures how many expected chunks were found. MRR rewards placing the first relevant result early. Faithfulness checks whether claims are supported by context.",
    code: `def recall_at_k(expected: set[str], retrieved: list[str], k: int) -> float:\n    if not expected:\n        return 1.0\n    return len(expected.intersection(retrieved[:k])) / len(expected)\n\nassert recall_at_k({\"c1\", \"c3\"}, [\"c2\", \"c3\", \"c5\"], 3) == 0.5`
  },
  {
    pattern: /function calling|tool use|tool schema|tool-call|tool errors|one tool|docstring|structured data|fallback/i,
    explanation: "Tool calling lets the model request a structured action instead of writing prose. The application validates the call, executes trusted code, and returns an observation. The schema is the model's instruction manual and the application's safety boundary.",
    mechanics: "Design one tool per coherent operation. Give arguments precise names and descriptions. Return typed data and explicit errors. Put retries and fallbacks inside tool code where they can be tested.",
    code: `from pydantic import BaseModel, Field\n\nclass SearchDocsArgs(BaseModel):\n    query: str = Field(description=\"Natural-language search query\")\n    top_k: int = Field(default=5, ge=1, le=10)\n\nasync def search_docs(args: SearchDocsArgs) -> list[dict]:\n    return await retriever.search(args.query, top_k=args.top_k)`
  },
  {
    pattern: /mcp|server|client|stdio|http transport|registry|auth model|resource/i,
    explanation: "MCP standardizes how AI clients discover tools, resources, and prompts from external servers. Instead of every AI app inventing a custom plugin layer, MCP gives a common protocol boundary.",
    mechanics: "An MCP client connects to an MCP server over a transport such as stdio or HTTP. The server lists capabilities, the client chooses calls, and the server returns structured results. Authentication and resource semantics must be designed carefully because tools can expose sensitive systems.",
    code: `# Minimal shape of an MCP-style tool function\nasync def list_recent_errors(service: str, minutes: int = 30) -> dict:\n    rows = await logs.query(service=service, window_minutes=minutes)\n    return {\"service\": service, \"errors\": rows}`
  },
  {
    pattern: /react|reasoning \+ acting|thought|action|observation/i,
    explanation: "ReAct is an interaction pattern: reason about the next step, choose an action, observe the result, and repeat until the answer is ready. In production, the private reasoning text may be hidden, but the action-observation loop remains.",
    mechanics: "Bound the loop with maximum steps, maximum cost, and termination conditions. Prefer deterministic routing when the next action is obvious.",
    code: `for step in range(MAX_STEPS):\n    decision = await model.decide(state)\n    if decision.kind == \"final\":\n        return decision.answer\n    observation = await run_tool(decision.tool, decision.arguments)\n    state.observations.append(observation)\nraise RuntimeError(\"agent exceeded step budget\")`
  },
  {
    pattern: /langchain|create_agent|@tool|parallel tool|structured outputs|humanintheloop|checkpoint|approval/i,
    explanation: "Agent frameworks package common mechanics: model adapters, tool schemas, middleware, checkpointers, structured output parsing, and tracing. They are useful when they make the control flow clearer rather than hiding it.",
    mechanics: "Human-in-the-loop middleware pauses execution before sensitive operations. Checkpoints persist state so the workflow can resume after approval, failure, or deployment restart.",
    code: `class ApprovalRequired(Exception):\n    def __init__(self, action: str, payload: dict):\n        self.action = action\n        self.payload = payload\n\n\ndef require_approval(action: str, payload: dict):\n    if action in {\"send_email\", \"charge_card\", \"delete_record\"}:\n        raise ApprovalRequired(action, payload)`
  },
  {
    pattern: /computer use|operator|apps sdk|browser-use|stagehand|playwright|mouse|screenshot/i,
    explanation: "Computer-use agents operate through visual or DOM interfaces when no stable API exists. They are slower and riskier than API integrations because perception, clicking, and page state can fail.",
    mechanics: "Use browser automation for legacy systems, manual workflows, and UI-only products. Add sandboxes, screenshots, action logs, confirmations, and strict domain allowlists.",
    code: `# Prefer API calls when possible. Use browser automation when the UI is the API.\nawait page.goto(\"https://internal.example.com/report\")\nawait page.get_by_label(\"Start date\").fill(\"2026-01-01\")\nawait page.get_by_role(\"button\", name=\"Generate\").click()`
  },
  {
    pattern: /context window|working memory|forget|token budget|recency bias|system|context|user separation|dynamic_prompt/i,
    explanation: "The context window is the only information the model can condition on during a request. Structure matters because instructions, evidence, memory, and user text have different trust levels.",
    mechanics: "Keep system instructions stable, put retrieved evidence in a clearly delimited context section, preserve the latest user request verbatim, and avoid mixing untrusted text with instructions.",
    code: `prompt = f\"\"\"\n<SYSTEM>{system_rules}</SYSTEM>\n<CONTEXT>{retrieved_evidence}</CONTEXT>\n<HISTORY>{recent_history}</HISTORY>\n<USER>{user_message}</USER>\n\"\"\"`
  },
  {
    pattern: /sliding window|message-pair|tool calls in history|session history/i,
    explanation: "Short-term memory is the recent conversation state. Preserving user/assistant pairs keeps references coherent; dropping one half can make later answers nonsensical.",
    mechanics: "Keep the newest turns verbatim, summarize older turns only when needed, and decide whether tool observations remain useful or should be replaced by compact facts.",
    code: `def last_message_pairs(messages, pairs=5):\n    tail = messages[-pairs * 2:]\n    if tail and tail[0][\"role\"] == \"assistant\":\n        tail = tail[1:]\n    return tail`
  },
  {
    pattern: /semantic caching|similarity threshold|cache hit|daemon-thread|sub-millisecond/i,
    explanation: "Semantic caching reuses an answer when a new query is close enough to a previous query. It reduces cost and latency, but the threshold controls risk: too low returns wrong answers; too high misses savings.",
    mechanics: "Cache only when inputs, permissions, data version, and output requirements match. Store embedding, answer, source version, and safety metadata together.",
    code: `if similarity(query_embedding, cached.embedding) > 0.97 and cached.index_version == current_version:\n    return cached.answer`
  },
  {
    pattern: /episodic|long-term|user profile|preferences|managed memory|mem0|zep|privacy|gdpr|right-to-be-forgotten|compression|summarise|summarizes/i,
    explanation: "Longer-term memory stores facts beyond the current request: user preferences, prior decisions, episodic events, and durable profile data. Memory is powerful because it personalizes behavior, and dangerous because it can retain sensitive information.",
    mechanics: "Separate memory types by purpose and retention period. Make deletion possible. Compress only information that does not need exact wording.",
    code: `class MemoryRecord(BaseModel):\n    user_id: str\n    kind: Literal[\"preference\", \"fact\", \"episode\"]\n    text: str\n    source_event_id: str\n    expires_at: datetime | None`
  },
  {
    pattern: /multi-agent|supervisor|workers|sequential|fan-out|fan-in|plan-and-execute|reflection|agent-as-tool|stategraph|reducers|conditional edges|cycles|state management|checkpointers|a2a|frameworks|debugging/i,
    explanation: "Multi-agent orchestration decomposes work into nodes with explicit state transitions. A planner may create tasks, workers may perform specialized operations, a synthesizer may merge results, and a validator may decide whether to retry or finish.",
    mechanics: "Define state before prompts. The graph should make allowed transitions visible. Checkpoints make long workflows resumable and debuggable.",
    code: `class GraphState(BaseModel):\n    question: str\n    plan: list[str] = []\n    partial_results: list[str] = []\n    final_answer: str | None = None\n\n\ndef route_after_validation(state: GraphState) -> str:\n    return \"final\" if state.final_answer else \"repair\"`
  },
  {
    pattern: /input guardrails|output guardrails|action guardrails|bedrock guardrails|contextual grounding|automated reasoning|harmful|topic blocking|observability|langsmith|langfuse|token cost|latency|failure rate|production evaluation|feedback|drift/i,
    explanation: "Guardrails are layered controls. Input guardrails reject or transform unsafe requests before model execution. Output guardrails check the answer before release. Action guardrails constrain tools that can affect external systems.",
    mechanics: "Use deterministic checks for hard rules and LLM judges for semantic judgments. Observability must connect user request, retrieved chunks, prompts, model output, tool calls, latency, cost, and evaluation scores.",
    code: `def validate_sql(query: str) -> None:\n    lowered = query.lower()\n    forbidden = [\"insert\", \"update\", \"delete\", \"drop\", \"alter\"]\n    if any(word in lowered for word in forbidden):\n        raise PermissionError(\"only read-only SQL is allowed\")`
  },
  {
    pattern: /s3|storage|lambda|ecs|fargate|ecr|vpc|subnet|security group|iam|api gateway|bedrock|agentcore|vertex|azure|docker|task definition|secrets manager|promotion|sse|websocket|load testing|k6|locust|model routing|max-tokens|capacity/i,
    explanation: "Cloud primitives host the AI service around the model. Object storage holds documents, databases hold state, containers or functions run code, gateways expose APIs, IAM controls permissions, and observability systems record behavior.",
    mechanics: "Choose Lambda for short event-driven tasks and containers for long-running agents or heavier dependencies. Stream with SSE for one-way token delivery; use WebSockets when the client must send messages during the stream.",
    code: `FROM python:3.13-slim\nWORKDIR /app\nCOPY pyproject.toml uv.lock ./\nRUN pip install uv && uv sync --frozen\nCOPY . .\nCMD [\"uv\", \"run\", \"uvicorn\", \"app:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8080\"]`
  }
];

function lessonFor(text) {
  return patternLessons.find((lesson) => lesson.pattern.test(text)) || {
    explanation: "This concept is part of the engineering vocabulary used to build reliable AI systems. Treat it as a mechanism with inputs, outputs, constraints, and failure modes rather than as a keyword to memorize.",
    mechanics: "Define the data shape, decide where validation occurs, add observability, and write the smallest implementation that proves the behavior.",
    code: `# Minimal engineering loop\ninput_data = validate(raw_input)\nresult = run_operation(input_data)\nrecord_metric(\"operation.success\", True)\nreturn serialize(result)`
  };
}

function codeBlock(code, lang = "python") {
  return `<pre><code class="language-${lang}">${escapeHtml(code.trim())}</code></pre>`;
}

function diagramBlock(text) {
  return `<pre class="diagram"><code>${escapeHtml(text.trim())}</code></pre>`;
}

function moduleExample(phase, section) {
  const haystack = `${section.title} ${section.items.join(" ")}`;
  const lesson = lessonFor(haystack);
  if (/cypher|neo4j/i.test(haystack)) return codeBlock(lesson.code, "cypher");
  if (/docker/i.test(haystack)) return codeBlock(lesson.code, "dockerfile");
  return codeBlock(lesson.code, "python");
}

function renderConceptItem(item) {
  const lesson = lessonFor(item);
  return `
    <section class="concept">
      <h3>${escapeHtml(item)}</h3>
      <p>${escapeHtml(lesson.explanation)}</p>
      <p>${escapeHtml(lesson.mechanics)}</p>
    </section>
  `;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function slug(value) {
  return String(value).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function moduleId(section) {
  return `module-${section.n.replace(".", "-")}-${slug(section.title)}`;
}

function capstoneId(capstone) {
  return `capstone-${capstone.n}`;
}

function allPages() {
  const pages = [{ id: "", title: "Introduction", type: "home" }];
  curriculum.forEach((phase) => {
    pages.push({ id: `phase-${phase.id}`, title: phase.title, phase, type: "phase" });
    phase.sections.forEach((section) => pages.push({ id: moduleId(section), title: `${section.n} ${section.title}`, phase, section, type: "module" }));
  });
  capstones.forEach((capstone) => pages.push({ id: capstoneId(capstone), title: `Capstone ${capstone.n}: ${capstone.title}`, capstone, type: "capstone" }));
  pages.push({ id: "hosting", title: "Hosting", type: "hosting" });
  return pages;
}

const pages = allPages();

function link(id, label) {
  return `<a href="#/${id}">${escapeHtml(label)}</a>`;
}

function list(items) {
  return `<ul>${items.map((item) => `<li>${item}</li>`).join("")}</ul>`;
}

function plainList(items) {
  return list(items.map(escapeHtml));
}

function explainItem(item, phase) {
  const found = topicExplanations.find(([pattern]) => pattern.test(item));
  const explanation = found ? found[1] : `Study this as a practical engineering habit, not as vocabulary. Write a small example, name the failure mode it prevents, and connect it to ${phase.title.toLowerCase()} work.`;
  return `<h3>${escapeHtml(item)}</h3><p>${explanation}</p>`;
}

function renderHome() {
  const totalModules = curriculum.reduce((sum, phase) => sum + phase.sections.length, 0);
  return `
    <h1>The AI Engineer Book</h1>
    <p class="lede">A technical book about the concepts behind production AI engineering: Python services, language models, prompting, retrieval, tools, memory, orchestration, guardrails, operations, and deployment.</p>
    <div class="meta">
      <span class="pill">${curriculum.length} phases</span>
      <span class="pill">${totalModules} modules</span>
      <span class="pill">${capstones.length} capstones</span>
      <span class="pill">Static site</span>
    </div>
    <img class="cover" src="assets/cover.png" alt="Technical study book cover artwork with notebook, code panels, and AI workflow diagrams">
    <p>AI engineering is the discipline of wrapping probabilistic models in deterministic software. The model can generate, reason, summarize, classify, and call tools, but the surrounding system must validate input, retrieve evidence, enforce permissions, observe behavior, and recover from failure.</p>
    <p>The chapters are ordered from language and service fundamentals toward full production systems. Each chapter explains the mechanism, shows how the pieces fit together, and introduces the failure modes that distinguish toy demos from real services.</p>
    <h2>Contents</h2>
    ${curriculum.map((phase) => `
      <section class="module-card">
        <h3>${link(`phase-${phase.id}`, `${phase.id}. ${phase.title}`)}</h3>
        <p>${escapeHtml(chapterGuides[phase.id].opening)}</p>
      </section>
    `).join("")}
    <h2>System model</h2>
    ${diagramBlock(`user interface\n    |\n    v\nAPI service -> context builder -> model call -> parser/validator -> response\n    |               |              |              |\n    v               v              v              v\ntraces          retrieval       tools          guardrails`)}
    <p>Most AI applications can be understood through this model. The interface captures intent. The API validates it. The context builder chooses what the model sees. The model produces text or tool calls. Parsers and guardrails convert the model output into application behavior. Traces and evaluations tell engineers whether the behavior is improving or getting worse.</p>
  `;
}

function renderPhase(phase) {
  const guide = chapterGuides[phase.id];
  return `
    <h1>${escapeHtml(phase.title)}</h1>
    <div class="meta"><span class="pill">${escapeHtml(phase.weeks)}</span><span class="pill">${escapeHtml(phase.weeksDetail)}</span></div>
    <p class="lede">${escapeHtml(guide.opening)}</p>
    <h2>Mechanics</h2>
    <p>${escapeHtml(guide.mechanics)}</p>
    <h2>Architecture</h2>
    ${diagramBlock(guide.diagram)}
    <h2>Chapter sections</h2>
    ${phase.sections.map((section) => `
      <section class="module-card">
        <h3>${link(moduleId(section), `${section.n} ${section.title}`)}</h3>
        ${plainList(section.items)}
      </section>
    `).join("")}
    <h2>Failure modes</h2>
    ${plainList(guide.failureModes)}
  `;
}

function renderModule(phase, section) {
  const guide = chapterGuides[phase.id];
  return `
    <h1>${escapeHtml(section.n)} ${escapeHtml(section.title)}</h1>
    <div class="meta"><span class="pill">${escapeHtml(phase.title)}</span><span class="pill">${escapeHtml(phase.weeks)}</span></div>
    <p class="lede">This section covers ${escapeHtml(section.title.toLowerCase())} as an engineering mechanism: the inputs it receives, the guarantees it creates, the implementation shape, and the ways it fails under production pressure.</p>
    <h2>Scope</h2>
    ${plainList(section.items)}
    <h2>Concept</h2>
    <p>${escapeHtml(guide.opening)}</p>
    <p>${escapeHtml(guide.mechanics)}</p>
    <h2>Process</h2>
    ${diagramBlock(guide.diagram)}
    <h2>Details</h2>
    ${section.items.map((item) => renderConceptItem(item)).join("")}
    <h2>Worked example</h2>
    <p>The example below shows the kind of small, explicit implementation that belongs in a production codebase: typed boundaries, clear control flow, and behavior that can be tested.</p>
    ${moduleExample(phase, section)}
    <h2>Production notes</h2>
    ${list([
      "Validate data at the boundary before it reaches model calls, tools, or storage.",
      "Record enough metadata to reproduce behavior later: model name, prompt version, input IDs, retrieved evidence, latency, cost, and errors.",
      "Prefer deterministic code for policy, routing, permissions, and parsing; use model calls for language tasks and judgment where deterministic code is insufficient.",
      "Design the failure path before the happy path is impressive."
    ])}
    <h2>Common defects</h2>
    ${plainList(guide.failureModes)}
  `;
}

function renderCapstone(capstone) {
  return `
    <h1>Capstone ${capstone.n}: ${escapeHtml(capstone.title)}</h1>
    <div class="meta"><span class="pill">${escapeHtml(capstone.phase)}</span><span class="pill">${escapeHtml(capstone.domain)}</span></div>
    <p class="lede">${escapeHtml(capstone.proves)}</p>
    <h2>System requirements</h2>
    ${plainList(capstone.build)}
    <h2>Architecture</h2>
    <pre><code>client -> API -> orchestration layer -> tools/retrieval/data stores
                 |                   |
                 v                   v
              tracing              eval logs</code></pre>
    <h2>Stack</h2>
    ${plainList(capstone.stack)}
    <h2>Engineering constraints</h2>
    ${list([
      "All external calls have timeouts and typed error paths.",
      "Every model call and tool call emits a trace with inputs, output shape, latency, and token cost.",
      "Retrieval and generation are evaluated separately.",
      "Sensitive actions are impossible without explicit approval in the application layer.",
      "Deployment configuration keeps secrets outside source control and container images."
    ])}
  `;
}

function renderHosting() {
  return `
    <h1>Hosting the Book</h1>
    <p class="lede">This site is plain static HTML, CSS, JavaScript, and images. There is no build step.</p>
    <h2>Fastest options</h2>
    ${list([
      "GitHub Pages: push the contents of this folder to a repository and enable Pages from the main branch.",
      "Netlify Drop: drag this folder into Netlify Drop.",
      "Cloudflare Pages: create a Pages project and point it at this folder."
    ])}
    <h2>Local preview</h2>
    <pre><code>cd ai-engineer-book
python3 -m http.server 8080</code></pre>
    <p>Then open <code>http://localhost:8080</code>.</p>
  `;
}

function renderPage(page) {
  if (!page || page.type === "home") return renderHome();
  if (page.type === "phase") return renderPhase(page.phase);
  if (page.type === "module") return renderModule(page.phase, page.section);
  if (page.type === "capstone") return renderCapstone(page.capstone);
  if (page.type === "hosting") return renderHosting();
  return renderHome();
}

function renderToc(filter = "") {
  const q = filter.trim().toLowerCase();
  const matches = (text) => !q || text.toLowerCase().includes(q);
  const groups = curriculum.map((phase) => {
    const moduleLinks = phase.sections
      .filter((section) => matches(`${phase.title} ${section.n} ${section.title} ${section.items.join(" ")}`))
      .map((section) => `<a class="toc-link" href="#/${moduleId(section)}">${escapeHtml(section.n)} ${escapeHtml(section.title)}</a>`)
      .join("");
    if (!moduleLinks && !matches(phase.title)) return "";
    return `<div class="toc-group"><a class="toc-heading" href="#/phase-${phase.id}">${escapeHtml(phase.id)}. ${escapeHtml(phase.title)}</a>${moduleLinks}</div>`;
  }).join("");
  const capstoneLinks = capstones
    .filter((capstone) => matches(`${capstone.title} ${capstone.domain} ${capstone.build.join(" ")}`))
    .map((capstone) => `<a class="toc-link" href="#/${capstoneId(capstone)}">Capstone ${capstone.n}: ${escapeHtml(capstone.title)}</a>`)
    .join("");
  document.querySelector("#toc").innerHTML = `
    <div class="toc-group">
      <a class="toc-heading" href="#/">Start</a>
      <a class="toc-link" href="#/">Introduction</a>
    </div>
    ${groups}
    <div class="toc-group">
      <span class="toc-heading">Capstones</span>
      ${capstoneLinks || ""}
      <a class="toc-link" href="#/hosting">Hosting</a>
    </div>
  `;
}

function currentId() {
  return decodeURIComponent(location.hash.replace(/^#\/?/, ""));
}

function activateToc(id) {
  document.querySelectorAll(".toc-link, .toc-heading").forEach((node) => {
    const nodeId = decodeURIComponent((node.getAttribute("href") || "").replace(/^#\/?/, ""));
    node.classList.toggle("active", nodeId === id);
  });
}

function renderPager(page) {
  const index = pages.findIndex((candidate) => candidate.id === page.id);
  const prev = pages[index - 1];
  const next = pages[index + 1];
  document.querySelector("#pager").innerHTML = `
    ${prev ? `<a href="#/${prev.id}">Previous<br><strong>${escapeHtml(prev.title)}</strong></a>` : "<span></span>"}
    ${next ? `<a href="#/${next.id}">Next<br><strong>${escapeHtml(next.title)}</strong></a>` : "<span></span>"}
  `;
}

function route() {
  const id = currentId();
  const page = pages.find((candidate) => candidate.id === id) || pages[0];
  document.querySelector("#page").innerHTML = renderPage(page);
  renderPager(page);
  activateToc(page.id);
  document.title = page.title === "Introduction" ? "The AI Engineer Book" : `${page.title} - The AI Engineer Book`;
  document.body.classList.remove("nav-open");
  document.querySelector("#menu").setAttribute("aria-expanded", "false");
  document.querySelector("#content").focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "instant" });
}

renderToc();
route();

window.addEventListener("hashchange", route);

document.querySelector("#search").addEventListener("input", (event) => {
  renderToc(event.target.value);
  activateToc(currentId());
});

document.querySelector("#menu").addEventListener("click", () => {
  const open = !document.body.classList.toggle("nav-open");
  document.querySelector("#menu").setAttribute("aria-expanded", String(!open));
});
