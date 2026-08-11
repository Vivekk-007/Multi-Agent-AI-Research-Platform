# Multi-Agent AI Research Platform

A FastAPI application that produces structured research reports through a LangGraph workflow, with TensorZero-backed model inference, AWS Bedrock safety checks, session and long-term memory, and an accompanying PyRIT red-team dashboard.

[Watch the demo video](https://youtu.be/zbdH9H5SBDI)

## Overview

The API accepts a research topic, validates it with Amazon Bedrock Guardrails, queues a job in Redis, and runs a four-stage agent workflow: search, summarize, write, and critique. Completed reports can be retrieved as text, JSON, or PDF. The service persists short-lived conversation history in Redis and stores report embeddings in PostgreSQL with pgvector to reuse closely matching work and provide related-report context.

The repository also contains a separate FastAPI-based PyRIT dashboard for running and inspecting red-team prompts against the research API.

## Key capabilities

- LangGraph workflow with `SearchAgent`, `SummarizeAgent`, `WriterAgent`, and `CriticAgent`.
- TensorZero gateway calls for the `research_summarize` and `report_write` functions.
- Amazon Bedrock Guardrails applied to both submitted topics and generated reports.
- Redis Streams job queue, result storage, per-IP rate limiting, session history, and semantic cache.
- PostgreSQL/pgvector long-term report memory, related-report context, and report diffs.
- Optional LangSmith tracing and LLM-based report evaluation.
- Text, JSON, and PDF report outputs.
- PyRIT dashboard for jailbreak, XPIA, Crescendo, and Skeleton Key attack scenarios.
- Terraform and GitHub Actions configuration for AWS ECS/Fargate deployment.

## Architecture

```mermaid
flowchart TB
    User[Browser or API client] -->|HTTP or optional HTTPS| ALB[Application Load Balancer]

    subgraph AWS[AWS deployment]
      ALB -->|port 80 or 443| App
      ALB -->|port 8001| PyRIT[PyRIT dashboard :8001]
      PyRIT -->|test requests| ALB
      subgraph Task[ECS Fargate app task]
        App[FastAPI API :8000]
        TZ[TensorZero sidecar :3000]
        App -->|inference| TZ
      end
      App -->|input and output checks| Guard[Amazon Bedrock Guardrails]
      App <--> Redis[(ElastiCache Redis)]
      App <--> DB[(RDS PostgreSQL + pgvector)]
      TZ --> Provider[Configured LLM provider]
      App -. optional traces .-> LangSmith[LangSmith]
      App --> Logs[CloudWatch Logs]
    end

    Secrets[Secrets Manager] -->|runtime configuration| App
```

### What each layer does

1. **Ingress and user interfaces.** The browser UI is served by the research API. Terraform also configures an Application Load Balancer in front of the ECS services. The PyRIT dashboard is a separate service that sends its test requests to the target API.
2. **API and request protection.** `POST /research` optionally authenticates the `X-API-Key`, enforces the per-IP Redis-backed rate limit, then calls Bedrock Guardrails on the submitted topic. Unsafe input is rejected before a job is created.
3. **Asynchronous work.** Accepted requests are added to a Redis Stream. The API process starts a worker loop at application startup, consumes the stream through a Redis consumer group, and writes a temporary result record keyed by job ID.
4. **Memory and reuse.** Before invoking agents, the worker checks a Redis semantic cache. On a cache miss it searches previous reports in PostgreSQL/pgvector. A close match can be reused; a related report can be supplied to the writer as context.
5. **Research workflow.** A LangGraph state machine invokes the four agents below. Each calls TensorZero's `/inference` endpoint. The critic can send the graph back to search for another pass.
6. **Model gateway.** In ECS, TensorZero runs in the same task as the FastAPI container, so the API connects to `http://localhost:3000`. TensorZero selects the configured provider/model and applies the repository's MiniJinja system templates.
7. **Result protection and delivery.** The service runs Bedrock Guardrails against the completed report. Approved content is cached, retained as session history, stored in long-term memory, and made available as text, structured JSON, or a generated PDF. Blocked output and processing failures are represented as distinct result states.
8. **Operations.** Terraform supplies CloudWatch log groups and optional LangSmith tracing. AWS Secrets Manager provides the runtime configuration; no application secret values are stored in this repository.

### Application lifecycle

Configuration is loaded when `app.config` is imported: the process reads the JSON secret named `research-agent/config` from AWS Secrets Manager. During FastAPI startup, the application opens its Redis and PostgreSQL pools, creates the `vector` extension plus the report table/indexes when absent, compiles the LangGraph graph, and starts the Redis Streams worker loop. Shutdown closes Redis and the PostgreSQL pool.

If configuration, Redis, or PostgreSQL initialization fails, startup cannot complete. If an individual job fails after startup, the worker stores `{"status":"error","error":"..."}` under that job ID and acknowledges the stream message.

### Research execution flow

```mermaid
flowchart TD
    START --> Search
    Search --> Summarize
    Summarize --> Write
    Write --> Verify
    Verify -->|critic approves, or iteration limit reached| END
    Verify -->|critic rejects and attempts remain| Search
```

The graph increments its iteration count after each write. A critic rejection routes back to search while the count is below `AGENT_MAX_ITERATIONS`; the configured default is `2`.

The graph state is a `TypedDict` named `ResearchState`:

| Field | Purpose |
| --- | --- |
| `topic`, `session_id` | Request identity and research subject. |
| `session_history` | Recent Redis conversation messages supplied to the search agent. |
| `ltm_context` | A related prior report supplied to the writer when available. |
| `search_results`, `summaries`, `report` | Artifacts passed from search through report generation. |
| `verified`, `iterations` | Critic decision and loop-control state. |
| `error` | Present in the state schema; the current graph nodes do not populate it. |

### Agent responsibilities

| Agent | Input | Work performed | Output |
| --- | --- | --- | --- |
| `SearchAgent` | Topic and up to four recent session messages | Requests five key facts, developments, and relevant details. | Research findings. |
| `SummarizeAgent` | Search findings | Condenses findings into structured bullet points. | Summary. |
| `WriterAgent` | Topic, summary, and optional related report | Produces the report with Executive Summary, Key Findings, Analysis, and Conclusion. | Draft report. |
| `CriticAgent` | Truncated draft report | Requests a factual-consistency and logical-coherence verdict. | Approval or rejection. |

## Components

| Component | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI app, lifecycle management, endpoints, worker loop, job processing. |
| `app/agents.py` | LangGraph state graph and the research, summary, writer, and critic agents. |
| `app/guardrails.py` | Applies Bedrock Guardrails to input and output. |
| `app/cache.py` | Redis semantic cache using `all-MiniLM-L6-v2` embeddings. |
| `app/memory.py` | Redis session history and PostgreSQL/pgvector long-term report memory. |
| `app/queue.py` | Redis Streams enqueue, consume, acknowledge, and result helpers. |
| `app/eval.py` | Per-report and batch LLM evaluation helpers, with LangSmith datasets when configured. |
| `tensorzero/` | TensorZero configuration, templates, and gateway image. |
| `pyrit_dashboard/` | Separate FastAPI PyRIT red-team dashboard. |
| `terraform/` | AWS infrastructure definitions. |

## Request processing and data flow

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI API
    participant G as Bedrock Guardrails
    participant R as Redis
    participant M as pgvector memory
    participant L as LangGraph / TensorZero

    C->>A: POST /research
    A->>G: validate input
    alt blocked input
        G-->>C: 400 safety response
    else accepted input
        A->>R: append job to Redis Stream
        A-->>C: job_id and session_id
        A->>R: consume job and load session history
        A->>R: semantic cache lookup
        alt cache hit
            R-->>A: cached report
        else cache miss
            A->>M: exact/related report lookup
            alt close LTM match
                M-->>A: stored report
            else no close match
                A->>L: execute graph
                L-->>A: draft report
                A->>G: validate output
            end
        end
        A->>R: store result and session message
        A->>M: store report embedding
    end
```

The research endpoint returns immediately after enqueueing. The same API process consumes the job asynchronously; clients poll `/result/{job_id}` for the status. The worker creates a background LLM evaluation after a successful result is stored, so evaluation does not block result delivery.

## Memory and context management

| Store | Data | How it is used | Retention behavior |
| --- | --- | --- | --- |
| Redis session list | `role` and truncated `content` records | The worker loads the session before cache/LTM decisions; the search agent receives the last four messages. | `SESSION_TTL`, default 1,800 seconds; list capped by `SESSION_MAX_MESSAGES`, default 5. |
| Redis semantic cache | Report text and an embedding keyed by a hash of the query | A cosine-similarity check can return a cached report before the graph runs. | `CACHE_TTL`, default 3,600 seconds. |
| Redis Stream and result keys | Queued job fields and JSON job results | Decouples request acceptance from processing and supports polling. | Results use `RESULT_TTL`, default 3,600 seconds. |
| PostgreSQL + pgvector | Topic, report, 384-dimensional embedding, creation time | Finds close matches for reuse, related reports for writer context, and prior reports for diffs. | The current schema has no automatic report deletion. Exact-match lookup restricts candidates to `LTM_DAYS`, default 7. |

Both the semantic cache and long-term memory embed topics with `all-MiniLM-L6-v2`. The application Docker image downloads this model during build and enables `HF_HUB_OFFLINE=1` at runtime. A PostgreSQL `ivfflat` cosine index is created with `IVFFLAT_LISTS` (default `100`).

## TensorZero and model configuration

TensorZero binds to `0.0.0.0:3000` and exposes the gateway that the agents call with `POST {TENSORZERO_URL}/inference`. Each call sends a function name and a chat-style message payload; the application reads response text from `content[0].text`.

| Function | Used by | Model route | Generation settings | System template |
| --- | --- | --- | --- | --- |
| `research_summarize` | Search, summary, critic, and LLM judges | `research_model` | temperature `0.3`, max tokens `2000` | `research_summarize_system.minijinja` |
| `report_write` | Writer | `research_model` | temperature `0.5`, max tokens `4000` | `report_write_system.minijinja` |

`research_model` lists `groq_primary` followed by `groq_fallback`. The configured provider models are `openai/gpt-oss-120b` and `llama-3.1-8b-instant`, respectively. Terraform injects `GROQ_API_KEY` only into the TensorZero sidecar from Secrets Manager. The system templates define the research-analysis style and the writer's required report sections; application code provides the user message, session context, findings, and related-report context.

## Report evaluation

Every successful job starts `evaluate_report` in the background. Four LLM-as-judge checks use `research_summarize` to score relevance, completeness, hallucination risk, and overall quality. Scores are parsed from `SCORE: X/10` and normalized to a 0-1 value; they are not returned with the original job result. The `/evaluate/{job_id}` endpoint runs the same evaluation on demand.

When LangSmith is configured, evaluation attempts to create or reuse `LANGSMITH_DATASET` and records a topic, report preview, job ID, and scores. LangSmith write failures are logged as warnings and do not fail the evaluation result.

## Technology stack

- Python 3.11 or later, FastAPI, Uvicorn, Pydantic
- LangGraph and LangChain Core
- TensorZero
- Redis and Redis Streams
- PostgreSQL with the `vector` extension, `asyncpg`, and `sentence-transformers`
- Amazon Bedrock Guardrails, AWS Secrets Manager, ECS/Fargate, ECR, ElastiCache, RDS, CloudWatch, and Application Load Balancer
- PyRIT 0.14
- Terraform and GitHub Actions

## Prerequisites

- Python 3.11+
- `uv` (the application Docker image uses it, and `uv.lock` is committed)
- AWS credentials that can read the `research-agent/config` secret and call Bedrock Guardrails
- Redis, PostgreSQL with pgvector, and a reachable TensorZero gateway
- A configured TensorZero model-provider credential (the supplied Terraform passes a Groq credential to the TensorZero sidecar)

The application loads its runtime configuration only from AWS Secrets Manager. This repository does not include a `.env.example`, Compose file, or a local service bootstrap, so a fully local runtime requires provisioning those dependencies and the expected secret outside this repository.

## Installation and local launch

Install the locked project dependencies:

```powershell
uv sync
```

Make the `research-agent/config` secret available in the selected AWS region. At minimum it must supply:

```text
BEDROCK_GUARDRAIL_ID
BEDROCK_GUARDRAIL_VERSION
REDIS_URL
DATABASE_URL
TENSORZERO_URL
```

`API_KEY` is optional; authentication is disabled when it is empty. See [Configuration](#configuration) for the additional supported settings.

Launch the research API from the repository root:

```powershell
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The root page serves `index.html`. When accessed on `localhost`, the UI calls `http://localhost:8000`; when hosted elsewhere it uses the current origin. It stores the entered API key in the browser.

Run the PyRIT dashboard separately:

```powershell
uvicorn main:app --host 0.0.0.0 --port 8001
```

Run this command from `pyrit_dashboard/` after installing its dependencies:

```powershell
pip install -r requirements.txt
```

The dashboard uses `TARGET_URL` (default: `http://app:8000`) and optional `REDIS_URL` environment variables.

## Configuration

`app/config.py` obtains one JSON object from AWS Secrets Manager (`research-agent/config`) in `AWS_REGION` (default `us-east-1`). Required values are listed below; all remaining values have code defaults.

| Setting | Required | Purpose / default |
| --- | --- | --- |
| `BEDROCK_GUARDRAIL_ID` | Yes | Bedrock Guardrail identifier. |
| `BEDROCK_GUARDRAIL_VERSION` | Yes | Bedrock Guardrail version. |
| `REDIS_URL` | Yes | Redis connection string. |
| `DATABASE_URL` | Yes | PostgreSQL connection string. |
| `TENSORZERO_URL` | Yes | TensorZero gateway base URL. In the ECS task definition, this is `http://localhost:3000` because the gateway is a sidecar in the same task. |
| `API_KEY` | No | Enables `X-API-Key` authentication when non-empty. |
| `LANGSMITH_API_KEY` | No | Enables LangSmith tracing when non-empty. |
| `LANGCHAIN_PROJECT` | No | LangSmith project; default `research-agent`. |
| `LANGSMITH_DATASET` | No | LangSmith dataset; default `research-agent-reports`. |
| `CACHE_TTL`, `CACHE_SIMILARITY_THRESHOLD` | No | Semantic cache TTL and threshold; defaults `3600`, `0.85`. |
| `SESSION_TTL`, `SESSION_MAX_MESSAGES`, `SESSION_CONTENT_TRUNCATE` | No | Session settings; defaults `1800`, `5`, `500`. |
| `LTM_DAYS`, `LTM_THRESHOLD`, `LTM_DIFF_LIMIT`, `IVFFLAT_LISTS` | No | Long-term memory and pgvector index settings; defaults `7`, `0.88`, `5`, `100`. |
| `STREAM_KEY`, `CONSUMER_GROUP`, `CONSUMER_NAME`, `RESULT_TTL` | No | Redis job queue settings; defaults `research:jobs`, `workers`, hostname, `3600`. |
| `AGENT_REPORT_TRUNCATE`, `AGENT_MAX_ITERATIONS` | No | Critic input truncation and maximum write iterations; defaults `3000`, `2`. |
| `EVAL_REPORT_TRUNCATE`, `EVAL_COMMENT_TRUNCATE` | No | Evaluation truncation settings; defaults `1500`, `300`. |
| `LLM_MAX_RETRIES`, `LLM_RETRY_DELAY` | No | Retry settings; defaults `3`, `1.0`. |
| `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_WINDOW` | No | Per-IP research rate limit; defaults `10`, `60` seconds. |
| `DB_POOL_MIN`, `DB_POOL_MAX` | No | PostgreSQL connection-pool bounds; defaults `2`, `10`. |

Keep credentials out of source control. Terraform marks its API and provider-key inputs as sensitive; pass them as Terraform variables or through your secret-management workflow.

## API reference

Protected endpoints require `X-API-Key` only when `API_KEY` is configured.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Serves the browser UI. |
| `GET` | `/health` | Returns service status and Redis connectivity. |
| `POST` | `/research` | Validates and queues a research job. Rate limited per client IP. |
| `GET` | `/result/{job_id}` | Returns `pending`, completed output, blocked, or error state. |
| `GET` | `/result/{job_id}/pdf` | Downloads a completed report as a PDF. |
| `GET` | `/session/{session_id}` | Returns stored session messages. |
| `GET` | `/diff/{topic}` | Returns a diff between the two newest reports for the topic, if available. |
| `GET` | `/stats` | Returns Redis statistics plus TensorZero URL and guardrail ID. |
| `GET` | `/evaluate/{job_id}` | Runs report evaluation for a completed job. |
| `POST` | `/run-evaluation` | Starts a background batch evaluation for supplied or recent topics. |

### Submit and retrieve a job

```bash
curl -X POST http://localhost:8000/research \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"topic":"What is AI?","output_format":"text"}'
```

The request body accepts `topic` (required), `session_id` (optional), and `output_format` (the UI offers `text`, `json`, and `pdf`). A successful submission returns a generated `job_id` and `session_id`. Poll `GET /result/{job_id}` until the response is no longer `{"status":"pending"}`.

`POST /research` may return `400` for guardrail-blocked input, `401` for an invalid or missing configured API key, or `429` for a rate-limited client. `GET /result/{job_id}/pdf` returns `404` until a job is complete.

## Safety, reliability, and observability

### Security controls implemented

- **Optional API-key authentication:** When `API_KEY` is not empty, protected endpoints compare `X-API-Key` to its configured value and return `401` on mismatch. No authorization roles or user identity model are implemented.
- **Rate limiting:** The research endpoint increments a Redis key per `request.client.host` and returns `429` after the configured request count in the configured time window. Other endpoints are not rate limited by application code.
- **Content safety:** Bedrock Guardrails validate input before enqueueing and generated output before it is stored. Terraform configures high-strength filters for hate, violence, sexual content, insults, misconduct, and prompt attacks; deny topics for weapons, illegal activity, and self-harm; and PII handling for selected entities.
- **Secrets:** Runtime application configuration comes from AWS Secrets Manager. Terraform supplies the TensorZero provider key through the sidecar's ECS secret mapping rather than a source-controlled environment variable.
- **Network boundaries:** The Terraform security groups allow the ALB to reach ECS task ports, and allow ECS tasks to reach Redis and RDS. Redis and RDS are declared in private subnets. ECS services themselves are configured in public subnets with public IP assignment.
- **CORS:** The FastAPI app explicitly allows all origins, `GET`/`POST`, and headers. Deployers should assess whether that is appropriate for their deployment.

### Failure behavior and observability

- Guardrail and TensorZero requests use configurable exponential backoff. TensorZero errors classified as permanent provider request errors are not retried; rate-limit responses may use a parsed retry-after interval.
- TensorZero provider diagnostic text is truncated and uses a pattern-based redaction for common secret field names before it is logged.
- A worker exception produces an `error` job result; an output guardrail intervention produces a `blocked` result. The `GET /health` endpoint reports `degraded` when Redis cannot be pinged.
- The API emits JSON-formatted application logs. Terraform configures CloudWatch log groups for the app, TensorZero sidecar, and PyRIT dashboard.
- When `LANGSMITH_API_KEY` is present, the application enables LangSmith tracing for agent and evaluation paths.

The implementation does not make a guarantee that every unsafe or adversarial request will be blocked. The PyRIT dashboard’s “passed” and “blocked” states are test outcomes, not a security certification.

## PyRIT red-team dashboard

The dashboard is a separate FastAPI service. It exposes `/run-attacks`, `/results`, `/status`, and `/`, and targets the research API through `TARGET_URL`. It defines prompt sets labeled Jailbreak, XPIA, Crescendo, and Skeleton Key. Each prompt is posted to `/research` with a new session ID, then the dashboard polls `/result/{job_id}` up to 40 times at three-second intervals.

PyRIT itself is initialized with SQLite memory once per dashboard process. The attack runner intentionally sends prompts through `ResearchAgentTarget` directly rather than PyRIT's `PromptSendingAttack`, as documented in the source for PyRIT 0.14 API compatibility. If `REDIS_URL` is configured, dashboard result rows are additionally stored under `pyrit:results` for seven days. The crescendo list stops when a turn is not passed.

Terraform also declares an EventBridge rule for 02:00 UTC every Monday that runs the PyRIT ECS task definition. The dashboard runs attacks only when its `/run-attacks` endpoint is called; the Terraform configuration does not define an HTTP call to that endpoint, so the scheduled task alone should not be interpreted as a completed automated attack run.

## Screenshots

### Research UI: guardrail enforcement

![Research UI showing a request blocked by the safety guardrail](docs/images/research-guardrail.png)

### Session history

![Research UI session history](docs/images/session-history.png)

### PyRIT red-team dashboard

![PyRIT dashboard overview](docs/images/pyrit-dashboard-overview.png)

![PyRIT dashboard attack results](docs/images/pyrit-dashboard-results.png)

### ECS deployment

![Amazon ECS cluster with research API and PyRIT services](docs/images/ecs-deployment.png)

## Containers and AWS deployment

`app/Dockerfile` builds the repository root with Python 3.11 and starts the API on port `8000`. `tensorzero/Dockerfile` builds the TensorZero gateway image, which binds to port `3000`. `pyrit_dashboard/Dockerfile` builds the separate dashboard and starts it on port `8001`.

Terraform provisions the AWS deployment components represented in `terraform/main.tf`, including VPC networking, an Application Load Balancer, ECS/Fargate task definitions and services, ECR repositories, ElastiCache Redis, RDS PostgreSQL, Secrets Manager, CloudWatch log groups, Bedrock Guardrails, IAM roles, and CPU-based auto-scaling for the research app. The research ECS task places the API and TensorZero containers together, which is why the service uses `http://localhost:3000` for its gateway.

To initialize the configured remote Terraform state, first create its S3 bucket and DynamoDB lock table with:

```bat
bootstrap.bat
```

Then review the plan and apply it from `terraform/` with the required image and secret inputs:

```powershell
terraform init
terraform plan
terraform apply
```

The repository’s GitHub Actions workflow builds and pushes the app, PyRIT, and TensorZero images to ECR on pushes to `main`, then updates or creates the ECS services. It requires repository secrets named `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`; its deployment process waits for app-service stability and contains rollback steps for both app and PyRIT service deployments.

## Testing

No automated test suite is present in the repository. The PyRIT dashboard is the included security-test interface; it is not a replacement for unit or integration tests.

## Known limitations

- There is no checked-in local Compose configuration or example Secrets Manager payload.
- `Config` fetches AWS Secrets Manager during module import, so the API cannot start without AWS access and a readable runtime secret.
- The Redis Streams worker runs in-process. Redis pending-message recovery is not implemented in the repository.
- Semantic-cache lookup scans stored embedding keys, which should be considered when sizing a Redis deployment.
- The repository has no automated tests or CI validation workflow beyond deployment.

## Project structure

```text
app/                 Research API, agents, persistence, guardrails, and Docker image
pyrit_dashboard/     PyRIT red-team dashboard and Docker image
tensorzero/          TensorZero gateway configuration and prompt templates
terraform/           AWS infrastructure
docs/images/         README screenshots
.github/workflows/   ECS deployment workflow
```
