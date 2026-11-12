# PROJECT JOURNAL — The Relay

> **Append-only engineering record.** This file is the project's memory. It records what
> was built, why, what was measured, what failed, and what is still unknown. Historical
> entries are never silently rewritten. When a decision changes, a new entry is appended
> and the old one stays visible.
>
> **Honesty rule.** Facts are labelled:
> - **[DOC]** verified from official documentation (URL + access date)
> - **[MEASURED]** produced by a real run, with the raw artifact named
> - **[SIM]** produced by the local stand-in
> - **[EST]** estimate or calculation, not a measurement
> - **[ASSUMPTION]** unverified belief
> - **[DECISION]** a deliberate engineering choice
> - **[UNKNOWN]** not yet established

---

## A. Project overview

### A.1 Objective

Build **The Relay**, a small research assistant that answers engineers' questions using
exactly three arXiv papers, cites where in the papers each claim comes from, refuses when
the papers do not contain the answer, and measures its own cost, latency, reliability, and
answer quality under controlled load.

### A.2 Assignment requirements (source of truth)

The brief is `C:\Users\Prachin\Downloads\The_Relay_Assignment_Brief.pdf` (4 pages).
It was extracted verbatim on 2026-11-10 and the text is preserved in
`docs/assignment_brief.txt`. The derived requirement matrix is
`ASSIGNMENT_REQUIREMENTS.md`.

The brief requires:

- A working pipeline (Python, C++, or Go) with **at least four steps**, **at least one
  tool call**, and **at least one branch or loop**. One step must retrieve passages.
- Exactly three papers at exactly these versions:
  1. *Attention Is All You Need*, arXiv 1706.03762v7
  2. *Moshi: a speech-text foundation model for real-time dialogue*, arXiv 2410.00037v1
  3. *Dynamic Chunking for End-to-End Hierarchical Sequence Modeling*, arXiv 2507.07955v2
- Real hosted model on **Groq's free tier**, own account and key; find limits from
  documentation and from responses at the limit.
- A controllable **stand-in model service** that can be slow, fail, refuse on a limit,
  slow down under load, and stream its reply in pieces.
- Calibrate the stand-in from real measurements and say where it differs most.
- Real probe first, then at least three load shapes; use the stand-in for loads the real
  service cannot take; keep raw results for both.
- A **two-page** report with fixed headings in a fixed order, plus **Tables 1, 2a, 2b, 3**.
- `results.csv` with exactly 13 columns and one row per request.
- ≥15 self-written evaluation questions, answer quality, tokens per answer.
- Honest Vaani feedback and real assistant-collaboration history.

### A.3 Primary optimization priorities

Two goals are actively optimised; the other two are what I trade away when they conflict.

1. **Quality** — evidence-grounded answers, accurate citations, correct refusal, no
   unsupported claims. Verification is never skipped to save tokens.
2. **Speed** — TTFT, total latency, queue wait, stage timings. Where speed and quality
   conflict, the request waits (up to `request_deadline_ms`) rather than degrading.
3. **Cost** (traded away) — one generation call, deterministic verification by default,
   refusal before any model call, exact cache.
4. **Availability** (traded away) — honest communication, never fabricated answers to stay
   up, no request kept alive past its deadline.

This order replaces the earlier cost-first ordering recorded in this journal; it is the
order the owner stated and the order `MINE.md` now uses.

The binding constraint discovered from the provider documentation is **TPM = 8,000
tokens/minute** and **TPD = 200,000 tokens/day**, not RPM = 30. This is recorded in
section G and shapes the whole design.

### A.4 Scope and non-goals

In scope: a modular Python application, local retrieval, deterministic scope/evidence
gates, one generation call per answer, deterministic citation verification, quota
accounting, bounded concurrency and queueing, retries, deadlines, a deterministic
stand-in, instrumentation, experiments, metrics, tests, and documentation.

Explicit non-goals (also report candidates for "two things I deliberately did not build"):

- No microservices, Kubernetes, message broker, or distributed database.
- No multi-agent decomposition. Extra agents multiply token cost on an 8k-TPM budget.
- No vector-database server. 120 pages fit in memory.
- No web UI. A CLI plus an in-process async API is enough for measurements.
- No semantic cache in the first build. Similarity is not equivalence, and a false hit is
  a confidently wrong answer. The exact cache is safe; semantic caching is deferred.
- No Redis in the first build. Redis is not installed on this machine and is not required
  by the brief. The exact cache is in-process and fail-open by construction. A Redis
  adapter can be added behind the same interface later.

### A.5 Current status

**Phase 0 (audit and planning): complete. Phase 1 (foundation): in progress.**
Real Groq measurements are **blocked**: no `GROQ_API_KEY` is present in the environment or
in a `.env` file. The adapter and probe are built so the owner can run them in one command,
but no real result will be claimed until they are actually run.

---

## B. Requirements tracking

The full matrix is in `ASSIGNMENT_REQUIREMENTS.md`. This section records only the state at
the end of each development session. Status vocabulary: Not started · In progress ·
Implemented · Tested · Verified · Blocked.

| Area | Status at 2026-11-10 (session 1) | Evidence |
|---|---|---|
| Corpus fetched, hashed, parsed | Verified | `documents/*.pdf`, `config/corpus_manifest.json`, `tests/test_ingest.py` |
| Ingestion, header stripping, chunking | Implemented | `the_relay/rag/ingest.py` |
| Local retrieval, 3 presets | Implemented | `the_relay/rag/index.py`, `config/retrieval.yaml` |
| Scope filter | Implemented | `the_relay/core/scope.py`, `tests/test_scope_filter.py` |
| Evidence gate | Implemented | `the_relay/core/evidence.py` |
| Generation + citations | Implemented | `the_relay/core/pipeline.py`, `the_relay/core/prompts.py` |
| Deterministic verification | Implemented | `the_relay/core/verify.py` |
| Groq adapter | Implemented, not exercised | `the_relay/providers/groq.py` |
| Stand-in | Implemented | `the_relay/providers/standin.py` |
| Quota manager | Implemented | `the_relay/core/quota.py` |
| Bounded concurrency + queue | Implemented | `the_relay/core/scheduler.py` |
| Retry policy | Implemented | `the_relay/core/retry.py` |
| Instrumentation (CSV + JSONL) | Implemented | `the_relay/obs/` |
| 15+ evaluation questions | Implemented | `evaluation/questions.json` |
| Experiments | Implemented (stand-in) | `results/` |
| Real probe | **Blocked** | no key |
| Two-page report | In progress | `reports/REPORT.md` |
| Vaani feedback | **Blocked** | owner must visit and author |
| Conversation history | In progress | `docs/conversation/` |

---

## C. Architecture history

### C.1 Initial architecture (2026-11-10, before coding)

The first plan proposed a modular monolith with Redis exact + semantic caches, fastembed
ONNX embeddings, BM25 + dense + RRF, a cross-encoder reranker, FastAPI, and a stand-in.

### C.2 Current architecture (2026-11-10, session 1)

```
User / load generator
        |
        v
  +-------------------+
  | ingress + normalize|  (deterministic, 0 tokens)
  +-------------------+
        |
        v
  +-------------------+
  | exact cache (opt) |  (in-process, versioned key, fail-open)
  +-------------------+
        |
        v
  +-------------------+
  | scope filter      |  (deterministic; obvious out-of-scope -> refusal, 0 tokens)
  +-------------------+
        |
        v
  +-------------------+
  | retrieval tool    |  (BM25 + TF-IDF + RRF + deterministic rerank; 3 presets)
  +-------------------+
        |
        v
  +-------------------+
  | evidence gate     |  (sufficient / partial / insufficient; loop <= 2)
  +-------------------+
        |
        v
  +-------------------+
  | admission + queue |  (quota manager, bounded concurrency, deadline)
  +-------------------+
        |
        v
  +-------------------+
  | generation        |  (1 Groq or stand-in call, streaming, cache write)
  +-------------------+
        |
        v
  +-------------------+
  | deterministic     |  (citation IDs, retrieval membership, claim support,
  | verification      |   numbers; optional LLM verifier behind a flag)
  +-------------------+
        |
        v
  +-------------------+
  | instrumentation   |  (results.csv, request_traces.jsonl, model_calls.jsonl)
  +-------------------+
```

Only the generation stage (and the optional LLM verifier) consumes provider tokens.
Everything else is local.

### C.3 Why this changed from C.1

- **Redis removed** because it is not installed, not required by the brief, and would make
  "cache works" an unverified claim. The exact cache is in-process and version-aware.
- **fastembed removed** because it is not installed and pulls a heavy ONNX/tokenizer stack.
  Retrieval uses BM25 + TF-IDF cosine, fused with RRF. This is a legitimate local retrieval
  method, has no model download, and is deterministic.
- **FastAPI removed** because the brief does not require HTTP and an in-process async API
  gives cleaner latency measurements (no network noise between load generator and pipeline).
- **Semantic cache deferred** to avoid a quality risk that would need a separate
  false-positive experiment.

These are recorded as ADR-006, ADR-007, ADR-008, ADR-009 below.

### C.4 Component responsibilities

| Component | File | Responsibility | Tokens? |
|---|---|---|---|
| Config | `the_relay/config.py` | every knob, typed; env + YAML | no |
| Normalise | `core/normalize.py` | safe cache key + meaning-preserving features | no |
| Scope | `core/scope.py` | deterministic out-of-scope refusal | no |
| Cache | `core/cache.py` | exact, versioned, TTL, in-process | no |
| Retrieval | `rag/index.py` | BM25 + TF-IDF + RRF + rerank | no |
| Evidence | `core/evidence.py` | sufficiency level and reason | no |
| Prompt | `core/prompts.py` | compact evidence-only prompt | no |
| Generation | `providers/*` | real or simulated model call | **yes** |
| Verify | `core/verify.py` | citation + claim support checks | no (LLM optional) |
| Quota | `core/quota.py` | RPM/RPD/TPM/TPD, reserve, in-flight | no |
| Scheduler | `core/scheduler.py` | bounded concurrency + bounded queue + deadline | no |
| Retry | `core/retry.py` | classified, bounded, jittered retries | indirect |
| Pipeline | `core/pipeline.py` | orchestration and traces | no |
| Instrumentation | `obs/*` | CSV + JSONL, UTC + monotonic | no |
| Stand-in | `providers/standin.py` | latency, failures, quotas, streaming | simulated |
| Metrics | `eval/metrics.py` | tables from raw CSV | no |

---

## D. Architecture decision records

### ADR-001 — Deterministic scope filter before any model call

- **Date:** 2026-11-10
- **Problem:** Out-of-scope questions must not consume tokens.
- **Options:** (a) LLM classifier; (b) keyword blocklist only; (c) deterministic rules +
  retrieval-based evidence gate.
- **Chosen:** (c).
- **Reason:** An LLM classifier costs a call on every question, including the ones we want
  to reject. A keyword-only filter would reject legitimate paraphrases. Deterministic rules
  catch obvious external-world questions; retrieval and the evidence gate resolve the rest.
- **Advantages:** zero cost for obvious refusals; no false rejection of unfamiliar
  terminology; auditable.
- **Disadvantages:** ambiguous questions still pay retrieval cost (local, zero tokens);
  rules need maintenance.
- **Cost impact:** removes model calls for clearly external questions.
- **Quality impact:** must be measured for false-positive refusals.
- **Speed impact:** faster for rejected questions.
- **Availability impact:** refusals are honest, not availability failures.
- **Validation:** `tests/test_scope_filter.py`; evaluation questions in the
  `out_of_scope` category.
- **Result:** pending measurement.

### ADR-002 — Local hybrid retrieval, no embedding API

- **Date:** 2026-11-10
- **Problem:** Retrieve relevant passages from 120 pages without API cost.
- **Options:** (a) dense embeddings via API; (b) fastembed ONNX; (c) BM25 only; (d) BM25 +
  TF-IDF + RRF + deterministic rerank.
- **Chosen:** (d).
- **Reason:** Technical papers contain exact tokens (`QK^T`, `Mimi`, `H-Net`, `RQD`) that
  dense retrieval can blur. BM25 handles exact terms; TF-IDF cosine handles some paraphrase;
  RRF avoids comparing incomparable scores. No model download, fully reproducible.
- **Advantages:** zero API cost, fast, deterministic, small dependency surface.
- **Disadvantages:** weaker paraphrase recall than a neural embedding; no semantic
  understanding. This is a real limitation.
- **Cost impact:** zero token cost.
- **Quality impact:** measured by retrieval hit rate and Table 3.
- **Speed impact:** milliseconds.
- **Validation:** `tests/test_retrieval.py`, retrieval hit rate on the eval set.
- **Result:** pending measurement.

### ADR-003 — One generation call, deterministic verification by default

- **Date:** 2026-11-10
- **Problem:** A second LLM verification call doubles token cost on an 8k-TPM budget.
- **Options:** (a) no verification; (b) deterministic only; (c) always LLM; (d)
  deterministic + optional LLM.
- **Chosen:** (d), with deterministic verification always on and LLM verification off by
  default (`verification_mode=deterministic`).
- **Reason:** Citation membership, existence, and lexical/numeric support can be checked
  locally. A second LLM call must earn its cost through measurement.
- **Advantages:** cheap, fast, catches malformed or unsupported citations.
- **Disadvantages:** lexical support is not proof of entailment; it can miss subtle
  contradictions. Documented as a limitation.
- **Cost impact:** avoids ~100% token increase by default.
- **Quality impact:** catches structural citation failures, not all semantic errors.
- **Validation:** `tests/test_verifier.py`; eval citation score; optional A/B experiment.
- **Result:** deterministic checks tested; LLM mode not measured.

### ADR-004 — Token-aware admission, not request-count admission

- **Date:** 2026-11-10
- **Problem:** RPM 30 looks like the scarce resource, but TPM 8,000 binds first for
  realistic prompts.
- **Options:** (a) count requests only; (b) token bucket on TPM only; (c) account for RPM,
  RPD, TPM, TPD and in-flight concurrency.
- **Chosen:** (c).
- **Reason:** A single 4,000-token request consumes half a minute of TPM. Counting requests
  would admit two such requests and then hit a 429. The quota manager estimates tokens
  before the call and reconciles actual usage after.
- **Advantages:** prevents self-inflicted 429s; makes daily exhaustion predictable.
- **Disadvantages:** estimates can be wrong; conservative estimates reduce throughput.
- **Cost impact:** avoids retry waste.
- **Quality impact:** none directly; protects capacity for evidence-backed answers.
- **Validation:** `tests/test_quota.py`, load scenarios.
- **Result:** pending measurement.

### ADR-005 — Honest refusal over fabricated availability

- **Date:** 2026-11-10
- **Problem:** Under overload or quota exhaustion, a system can either fabricate an answer
  or tell the truth.
- **Options:** (a) degrade quality silently; (b) return cached/partial answer without
  labelling; (c) explicit status message with no answer.
- **Chosen:** (c), plus labelled retrieval degradation when budget is low.
- **Reason:** The brief says it never wants a wrong or embarrassing answer to stay fast.
- **Advantages:** trust; measurable failure states.
- **Disadvantages:** users are turned away; availability is lower.
- **Validation:** scenario messages and tests.
- **Result:** implemented; measurement pending.

### ADR-006 — Remove Redis from the first build

- **Date:** 2026-11-10
- **Problem:** The earlier plan made Redis mandatory, but Redis is not installed and the
  brief does not require caching.
- **Options:** (a) install Docker/Redis; (b) keep an in-process exact cache; (c) no cache.
- **Chosen:** (b).
- **Reason:** The exact cache captures the largest safe win (repeat questions) with no
  operational dependency. A distributed cache adds ops surface without a measured need at
  this scale.
- **Consequences:** cache is per-process and lost on restart; no shared cache across
  workers. Documented as a limitation.
- **Files affected:** `the_relay/config.py`, `the_relay/core/cache.py`, `.env.example`,
  `requirements.txt`.
- **Validation:** `tests/test_cache.py`.

### ADR-007 — Replace fastembed with BM25 + TF-IDF

- **Date:** 2026-11-10
- **Problem:** fastembed is not installed; installing it adds a large dependency and model
  download, and dense retrieval is not required by the brief.
- **Options:** (a) install fastembed; (b) BM25 + TF-IDF; (c) BM25 only.
- **Chosen:** (b).
- **Reason:** zero download, deterministic, adequate for exact technical vocabulary;
  keeps the project reproducible in the four-day window.
- **Consequences:** weaker paraphrase retrieval. Recorded as a known limitation and a
  candidate improvement.
- **Validation:** retrieval hit rate on the eval set vs. a BM25-only baseline.

### ADR-008 — Remove FastAPI; use an in-process async API

- **Date:** 2026-11-10
- **Problem:** HTTP adds a network hop and dependency surface; the brief does not require it.
- **Options:** (a) FastAPI; (b) in-process async calls from a CLI/load generator.
- **Chosen:** (b).
- **Reason:** cleaner latency attribution and fewer moving parts.
- **Consequences:** no remote API; a future HTTP adapter can wrap the same `Relay` class.
- **Validation:** end-to-end tests call the same async interface the load generator uses.

### ADR-009 — Defer semantic caching

- **Date:** 2026-11-10
- **Problem:** Semantic similarity is not equivalence. A false hit serves a wrong answer
  instantly, the worst possible failure.
- **Options:** (a) build it now; (b) defer it and document why.
- **Chosen:** (b).
- **Reason:** it needs a false-positive evaluation that would consume build time and could
  not be validated on a real model without a key. The exact cache is safe and sufficient
  for the first build.
- **Consequences:** fewer cache hits; documented as a deliberate omission.
- **Validation:** exact-cache tests only.

---

## E. Complete code change history

### CH-001 — Existing foundation from the first commit

- **Date:** 2026-11-10 14:37 IST
- **Files:** `the_relay/__init__.py`, `the_relay/config.py`, `the_relay/core/normalize.py`,
  `config/retrieval.yaml`, `.env.example`, `.gitignore`, `requirements.txt`,
  `ASSIGNMENT_REQUIREMENTS.md`, `IMPLEMENTATION_PLAN.md`, `documents/*.pdf`
- **What:** project skeleton, typed config, safe normalisation, retrieval presets, corpus
  PDFs, requirements audit, implementation plan.
- **Status:** foundation only; no pipeline yet.

### CH-002 — Session 1 implementation (this session)

- **Date:** 2026-11-10
- **Files:** see `CHANGELOG.md` and the git log.
- **What:** the pipeline, retrieval, providers, reliability, instrumentation, evaluation,
  experiments, tests, and documentation described in section C.
- **Tests:** recorded in section N.
- **Status:** see section N.

---

## F. Configuration history

| Date | Setting | Previous | New | Reason | Expected effect | Observed effect |
|---|---|---|---|---|---|---|
| 2026-11-10 | `provider` | `standin` | `standin` | no key available | safe offline runs | stand-in used |
| 2026-11-10 | `max_concurrency` | 4 (plan) | 5 (master default) | match the brief's provisional default | more parallel work | to measure |
| 2026-11-10 | `max_queue_depth` | 32 (plan) | 16 | smaller, easier to reason about; prevents long waits | earlier rejection under overload | to measure |
| 2026-11-10 | `request_deadline_ms` | 20,000 | 25,000 | allow one 8k-TPM wait plus generation | fewer premature timeouts | to measure |
| 2026-11-10 | `generation_max_retries` | 2 (plan) | 1 | master prompt's "one controlled infrastructure retry" | less retry token waste | to measure |
| 2026-11-10 | `retrieval_preset` | balanced | balanced | keep | unchanged | — |
| 2026-11-10 | `chunk_words` light | 700 | 400 | reduce prompt tokens on 8k TPM | lower cost, maybe lower recall | to measure |
| 2026-11-10 | `chunk_words` balanced | 450 | 280 | same | same | to measure |
| 2026-11-10 | `chunk_words` extensive | 300 | 200 | smaller granules, more of them | more precise context, similar tokens | to measure |
| 2026-11-10 | `top_k` light/balanced/extensive | 3/5/9 | 3/5/8 | align with smaller chunks | lower cost | to measure |
| 2026-11-10 | `cache_enabled` | true | true | exact cache is safe | cost reduction on repeats | to measure |
| 2026-11-10 | `semantic_cache_enabled` | false | removed | deferred (ADR-009) | no false hits | — |
| 2026-11-10 | `verification_mode` | `llm_verification_enabled=false` | `deterministic` | explicit policy | no second call by default | — |

---

## G. Real provider documentation

**Source:** `https://console.groq.com/docs/rate-limits` and
`https://console.groq.com/docs/models`, accessed **2026-11-10**. [DOC]

### G.1 Free-plan limits for the selected model

| Model | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `openai/gpt-oss-120b` | 30 | 1,000 | 8,000 | 200,000 |
| `openai/gpt-oss-20b` | 30 | 1,000 | 8,000 | 200,000 |
| `qwen/qwen3.8-27b` | 30 | 1,000 | 8,000 | 200,000 |

The docs state that limits apply at the organisation level and can change. The account's
exact limits are shown at `https://console.groq.com/settings/limits` and in response
headers. **Documented limits are not authoritative for our account.** [DOC]

### G.2 Rate-limit headers (documented)

The docs list, with illustrative values:

- `retry-after` — seconds; **only present on 429**
- `x-ratelimit-limit-requests` — always RPD
- `x-ratelimit-limit-tokens` — always TPM
- `x-ratelimit-remaining-requests` — always RPD
- `x-ratelimit-remaining-tokens` — always TPM
- `x-ratelimit-reset-requests` — RPD reset
- `x-ratelimit-reset-tokens` — TPM reset

Important subtlety: the header names say "requests" and "tokens" but the documented
semantics are **per day** for requests and **per minute** for tokens. Our instrumentation
records the raw header values and interprets them with this mapping. [DOC]

### G.3 Pricing (developer plan, not free tier)

`openai/gpt-oss-120b`: **$0.15 per 1M input tokens, $0.60 per 1M output tokens**.
`openai/gpt-oss-20b`: **$0.075 input, $0.30 output**. [DOC]

The free tier is not billed in dollars. Any "cost per 1,000 answers" in the report is a
**normalised estimate** computed from these published prices, clearly labelled. It is not a
claim that a free request was charged. [DOC] + [EST]

### G.4 What is not documented / not yet observed

- Our account's exact limits: **[MEASURED]** RPM 30, TPM 8,000, RPD 1,000 (TPD 200,000
  is documentation only, not in headers).
- Whether our account has separate ITPM/OTPM limits: **[UNKNOWN]**; the docs say some
  organisations do and the limits page shows an "X in / Y out" breakdown.
- In-flight concurrency limit: **[UNKNOWN]**; the docs do not publish one. Our application
  cap is a design choice, not a provider limit.
- Recovery time after a 429: **[MEASURED]** ~2 s for RPM (429 body: "Please try again in 2s");
  TPM recovery 44–58 s (from `x-ratelimit-reset-tokens`).
- The exact 429 body shape for our account: **[MEASURED]** —
  `{"error":{"message":"Rate limit reached for model ... on requests per minute (RPM):
  Limit 30, Used 30, Requested 1. Please try again in 2s.", "type":"requests",
  "code":"rate_limit_exceeded"}}`. Recorded in `logs/probe.jsonl`.

---

## H. Real Groq experiment history

All real requests used `openai/gpt-oss-120b` on the free tier through
`https://api.groq.com/openai/v1`, with `RELAY_GROQ_REASONING_EFFORT=low`.
Timestamps are UTC.

| Experiment | Date | Requests | Concurrency | Result |
|---|---|---|---|---|
| `probe --requests 31` (RPM test) | 2026-11-11 | 31 | 1 (sequential) | 30/31 HTTP 200; 31st returned HTTP 429 "Limit 30, Used 30, Requested 1. Please try again in 2s." Recovery ~2 s (rolling window). Confirms RPM=30 is a hard limit. Cost ~2,914 tokens. |
| Single `ask` (A01) | 2026-11-11 06:20 | 1 | 1 | Verified answer with `[S1]`, 2,110 in / 59 out, TTFT 782 ms, total 875 ms. |
| Real balanced quality run (v1/v2) | 2026-11-11 06:14–06:29 | 10 written rows | 1 | **Aborted.** Four requests were rejected by the app-side quota manager because of a milliseconds/seconds deadline bug, and one timed out at the 60 s harness deadline during normal TPM pacing. Fixed; see L. |
| Real balanced quality run (v3) | 2026-11-11 06:31 onward | 23 planned | 1 | See section J for the final table. |

### H.1 What the real headers actually report

```text
x-ratelimit-limit-requests: 1000
x-ratelimit-limit-tokens:   8000
x-ratelimit-remaining-requests / -tokens
x-ratelimit-reset-requests / -tokens
```

The headers confirm the **documented** per-minute token limit of 8,000 and a
**1,000 requests per day** budget. They do **not** expose a per-minute request
limit directly; 30 RPM is documentation only. No `429` was observed in this
session, so the direct 429 body and the true recovery time are **not measured**.

### H.2 The one number that matters most

With a ~1,900–2,200 token prompt, the 8,000 TPM window admits only three calls
per minute. The fourth call waits **44–58 s** for `x-ratelimit-reset-tokens`.
That is the dominant cost of a real quality run, and it is invisible in the
provider's per-call latency (median 812 ms total).

---

## I. Stand-in calibration history

### I.1 First calibration (2026-11-11, after the real probe)

The stand-in was originally written before any key existed, so its parameters
were labelled provisional. It is now calibrated against **11 successful real
generation calls**, excluding application-side admission waiting.

| Parameter | Provisional | Calibrated | Evidence |
|---|---|---|---|
| `base_latency_ms` | 850 | **810** | Real total median 812 ms (n=11, min 735, max 1,781) |
| `latency_sigma` | 0.30 | 0.30 (kept) | Real max/median = 2.19, consistent with a log-normal σ of 0.30 |
| `ttft_fraction` | 0.35 | **0.77** | Real median TTFT share 703/812 = 0.77 (range 0.53–0.93) |
| `stream_chunk_ms` | 20 | **0** | After TTFT, the real stream finishes in ~0.1 s; a fixed 20 ms per 3-word piece added ~0.7 s of invented latency per answer |
| `failure_rate` | 0.02 | 0.02 (kept, **assumption**) | 0 failures in 14 real requests. Too few observations to estimate a rate; 2% is kept to exercise the path and is labelled low-confidence |
| `timeout_probability` | 0.01 | 0.01 (kept, **assumption**) | No real timeout observed |
| `rpm / tpm / rpd / tpd` | 30 / 8000 / 1000 / 200000 | unchanged | TPM and RPD confirmed by live headers; RPM and TPD are documentation only |

### I.2 Where the stand-in still differs most

1. **It is not an LLM.** It is a deterministic extractive selector. Its answers
   are quotations, so it cannot paraphrase, and its error mode is "quoted the
   wrong sentence", not "invented a fact".
2. **No admission-wait model.** The 44–58 s TPM wait is produced by the
   application's quota manager, which both providers share. The stand-in does
   not reproduce the provider's own internal queueing (not observed anyway).
3. **No tokenizer agreement.** Token counts come from a local estimator, not
   Groq's tokenizer, so stand-in `tokens_in/out` are approximate.
4. **No real quality distribution.** Stand-in quality measures the retrieval and
   verification pipeline, not a model.

### I.3 Known inaccuracies

- No semantic variance between paraphrases.
- No provider-side concurrency queue.
- Failure rates are assumptions, not measurements.

### I.4 Update rule

Any new real observation replaces the corresponding value here and in
`config/standin_calibration.json`, with the raw row cited.

---

## J. Experiment results

No experiment has been run yet at the time this section was created. Results are appended
after the runs, with the raw file path and the exact command.

---

## K. Retrieval and quality evaluation

To be appended after the evaluation run. The dataset is `evaluation/questions.json`; the
grader is `the_relay/eval/grader.py`; the methodology is documented in `README.md` and the
report.

---

## L. Bugs and debugging history

### BUG-001 — Moshi running header corrupts extracted text

- **Discovered:** during the Phase 0 audit by extracting the PDF and inspecting headings.
- **Root cause:** the Moshi PDF repeats its title in the page header; naive extraction
  treats it as body text and naive heading detection picks it up on ~every page.
- **Fix:** detect lines repeated on ≥30% of pages and remove them before chunking;
  strip page-number-only lines.
- **Regression test:** `tests/test_ingest.py::test_running_header_is_removed`.
- **Outcome:** fixed, test passes.
- **Remaining risk:** a legitimate repeated sentence could be removed if it appears on
  ≥30% of pages. The detector only removes short lines (<120 chars) that repeat exactly.

### BUG-002 — Real quality run rejected admissible requests (deadline unit mismatch)

- **Discovered:** during real balanced quality run v1/v2 (2026-11-11). Four requests
  were rejected `rate_limited` despite the account having quota.
- **Root cause:** `pipeline.ask` built `deadline_mono` in **milliseconds** while
  `QuotaManager.acquire` and `BoundedScheduler` compare against `time.monotonic()`
  in **seconds**. The logged deadline was ~90 million seconds, so the manager saw no
  time pressure and could not pace calls into the TPM window.
- **Fix:** convert explicitly: `deadline_mono = time.monotonic() + deadline_ms / 1000.0`.
- **Regression test:** `tests/test_quota.py`.

### BUG-003 — Real quality run rejected requests at the rolling-window boundary

- **Discovered:** same real run as BUG-002. `try_acquire` returned `wait_s == 0.0`
  at the exact 60-second boundary (an event still counted by inclusive `>=` but about
  to fall out). `acquire` treated `wait_s <= 0` as a hard failure, rejecting four
  admissible requests.
- **Fix:** when `wait_s <= 0`, sleep briefly and re-check instead of raising.
- **Regression test:** `tests/test_quota.py::test_zero_wait_boundary_is_retried_not_rejected`.

### BUG-004 — Double-counted in-flight token reservations

- **Discovered:** code review of `QuotaManager.try_acquire`. In-flight reservations
  were already recorded in `_token_events`, but `try_acquire` added
  `_reserved_tokens` on top when checking per-minute and per-day token limits.
- **Fix:** removed the `+ self._reserved_tokens` term; `_token_events` counts them once.
- **Regression test:** `tests/test_quota.py::test_release_reconciles_estimate_with_actual_usage`.

### BUG-005 — Grader marked correct answers as refusals

- **Discovered:** manual re-grade of stand-in answers with honest caveats
  ("the papers do not provide ... e.g. ..., ").
- **Root cause:** `eval/grader.py` checked refusal phrases before required facts, so
  "BLEU was 28.4 [S1]. The papers do not provide X." was graded `refused_wrongly`.
- **Fix:** facts decide an answerable question's grade first; a refusal only yields
  `refused_wrongly` when required facts are missing.
- **Regression test:** `tests/test_grader.py::test_correct_answer_with_caveat_is_still_correct`.

### BUG-006 — Full-width citation brackets caused a false verification failure

- **Discovered:** real Groq run v1; Groq emitted `【S1】` (full-width brackets).
- **Root cause:** `verify_answer` only recognised ASCII `[S1]`.
- **Fix:** `normalise_citation_brackets` maps U+3010/U+3011 full-width brackets to
  ASCII before verification.
- **Regression test:** `tests/test_verify.py::test_full_width_citation_brackets_are_normalised`.

### BUG-007 — Daily reset returned 0.0 when only tokens were preconsumed

- **Discovered:** `daily_exhausted` scenario reported "next capacity in 0.0s".
- **Root cause:** `_day_reset_s` looked only at request timestamps, ignoring token
  events from `preconsume`.
- **Fix:** reset calculations consider both request and token event timestamps.
- **Regression test:** `tests/test_quota.py::test_daily_token_limit_is_reported_separately`.

### BUG-008 — Scheduler rejected the first request when `max_queue_depth=0`

- **Discovered:** test `test_full_queue_is_rejected_immediately`.
- **Root cause:** `_waiting >= self.max_queue_depth` is `0 >= 0` = true even when a
  worker is free.
- **Fix:** only enforce the queue cap when `max_queue_depth > 0`.
- **Regression test:** `tests/test_scheduler.py`.

### BUG-009 — Out-of-scope refusal recorded `retrieval_setting="none"`

- **Discovered:** canonical stand-in Table 3 showed a phantom `none` setting with 3
  rows and silently removed those questions from every real preset.
- **Root cause:** `pipeline.ask` set `retrieval_setting="none"` for scope refusals
  instead of the preset the request was made under.
- **Fix:** record `preset.name` for scope refusals so Table 3 groups correctly.
- **Regression test:** `tests/test_metrics.py::test_out_of_scope_refusal_records_the_requested_preset`.

---

## J. Experiment results

Canonical stand-in runs (2026-11-11, `config/standin_calibration.json` calibrated to
11 real Groq calls; `logs/results.csv` + root `results.csv`):

| Scenario | Source | Requests | ok | rate_limited | timeout | error |
|---|---|---|---:|---:|---:|---:|
| steady | stand-in | 6 | 6 | 0 | 0 | 0 |
| burst | stand-in | 8 | 2 | 6 | 0 | 0 |
| overload | stand-in | 12 | 2 | 2 | 0 | 8 |
| reduced_quota | stand-in | 6 | 1 | 0 | 0 | 5 |
| daily_exhausted | stand-in | 4 | 0 | 4 | 0 | 0 |

Quality runs (one question at a time), all statuses `ok`:

| Setting | Source | Questions | Correct | Wrong | Refused correctly | Refused wrongly |
|---|---|---:|---:|---:|---:|---:|
| light | stand-in | 22 | 11 (50.0%) | 8 (36.4%) | 3 (13.6%) | 0 |
| balanced | stand-in | 22 | 13 (59.1%) | 6 (27.3%) | 3 (13.6%) | 0 |
| extensive | stand-in | 22 | 13 (59.1%) | 6 (27.3%) | 3 (13.6%) | 0 |

The `none` setting no longer appears; the 3 out-of-scope refusals (U01 × 3 presets)
are now counted under the preset they were asked under.

Real Groq quality run v4 (2026-11-11, `openai/gpt-oss-120b`, 23 questions):
**completed**. All 23 `status=ok`, 0 retries, 58,791 tokens in / 5,674 tokens out,
724.1 s wall clock. Grades: correct=12, refused_wrongly=7, refused_correctly=4,
wrong=0. Real rows merged into `logs/results.csv` (128 total) via
`scripts/merge_results.py`; `metrics --export` regenerated root `results.csv`;
`reconcile` reports 0 mismatches.

After grader softening (unicode hyphen normalisation, commit fce3ff1):
re-grading the same answers flips A04 and M01 from refused_wrongly to correct:
correct=14, refused_wrongly=5, refused_correctly=4, wrong=0. The raw CSV retains
the original 12/7/4 grades (measured at run time); the re-grade is the
post-hoc improvement.

| Setting | Source | Questions | Correct | Wrong | Refused correctly | Refused wrongly | Tokens/answer | TTFT p50/p95 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| balanced | real | 23 | 12 (52.2%) | 0 (0.0%) | 4 (17.4%) | 7 (30.4%) | 2,802.8 | 1,516 / 58,281 |
| light | stand-in | 23 | 11 (47.8%) | 8 (34.8%) | 4 (17.4%) | 0 (0.0%) | 1,426.5 | 62 / 156 |
| balanced | stand-in | 23 | 13 (56.5%) | 6 (26.1%) | 4 (17.4%) | 0 (0.0%) | 2,063.0 | 63 / 219 |
| extensive | stand-in | 23 | 13 (56.5%) | 6 (26.1%) | 4 (17.4%) | 0 (0.0%) | 2,483.1 | 63 / 313 |

### Real Groq run v3 (aborted)

Ten rows written before the run was stopped to apply the 120 s deadline fix.
A01 correct, A02 correct, A03 correct (TTFT 469 ms, total 58,860 ms — admission
wait for TPM), A04 wrong, A05 correct, A06 wrong, A07 correct (TTFT 56,656 ms —
admission wait dominated). No 429 observed. See section H for header evidence.

Ten rows written before the run was stopped to apply the 120 s deadline fix.
A01 correct, A02 correct, A03 correct (TTFT 469 ms, total 58,860 ms — admission
wait for TPM), A04 wrong, A05 correct, A06 wrong, A07 correct (TTFT 56,656 ms —
admission wait dominated). No 429 observed. See section H for header evidence.

---

## K. Retrieval and quality evaluation

To be appended after the evaluation run. The dataset is `evaluation/questions.json`; the
grader is `the_relay/eval/grader.py`; the methodology is documented in `README.md` and the
report.

---

## M. AI assistant collaboration history

This project was built with an AI coding assistant. The journal records the collaboration
honestly:

- The assistant proposed the initial architecture, the requirement matrix, and the
  implementation plan in `IMPLEMENTATION_PLAN.md`.
- The assistant then proposed removing Redis, fastembed, and FastAPI to reduce unverified
  dependencies. **This was an assistant proposal, recorded in ADR-006/007/008; the project
  owner has not yet confirmed it.** The code follows the simpler design so it can be tested
  now; the owner may reverse it.
- The assistant has not invented any human decision, Vaani opinion, or real measurement.
- Sections that require the owner's judgement are marked **Blocked** or **Pending owner**.

The three most important prompts, the one assistant error caught, and the one overruled
decision are written from the recorded session history at the owner's request, in
`reports/REPORT.md` section 11. They cite only events that left a trace: the
`has Section 1 commands: False` self-check and the restore from `git show
HEAD~1:RUNS.md`, the 31-request RPM probe, the 23-question real run, and ADR-001/ADR-003.
The first two prompts are paraphrases — the original chat text was not recoverable — and
the third is quoted from the brief; nothing is invented.

---

## N. Current system state

### N.1 Current state (session 2, 2026-11-11)

- `GROQ_API_KEY` is present in gitignored `.env`; `RELAY_PROVIDER` defaults to `standin`.
- Configuration history rewritten in `.env.example` (matches actual `Settings`; Redis/semantic-cache variables removed).
- Stand-in calibrated against 11 successful real Groq calls: base_latency 810 ms,
  TTFT fraction 0.77, stream_chunk_ms 0. Calibration table in section I and
  `config/standin_calibration.json`.
- 89 automated tests pass (`pytest -q`, ~3.5 s), no network/quota. `pytest.ini`
  enforces `asyncio_mode = auto`.
- Canonical stand-in runs complete: `logs/results.csv` + root `results.csv` have
  105 rows across quality (69) + steady/burst/overload/reduced_quota/daily_exhausted.
- Real Groq quality run v4 (`openai/gpt-oss-120b`, 23 questions, 120 s deadline)
  is **in progress** in `C:\Users\Prachin\AppData\Local\Temp\opencode\relay_real_balanced_v4`.
- Bugs fixed: BUG-002 through BUG-009 (see section L), each with a regression test.
- `scripts/merge_results.py` added to merge real rows into the canonical CSV without
  duplicate request_ids.
- Report `reports/REPORT.md` not yet written; needs real v4 numbers before filling.

### N.2 Blocked / owner-authored items

- Vaani feedback: `reports/REPORT.md` section 11 holds the owner's first-hand review of the
  product at https://www.vaani.dev/ (providers/voices/custom voice, the hold-call and
  entity-extraction tools, and number verification at registration), verbatim as
  dictated, typos included. The earlier brief-feedback draft and its 11.1/11.2 split were
  removed at the owner's request; both remain in git history.
- Collaboration history (section M): written from the recorded session history at the
  owner's request and set as `reports/REPORT.md` section 11. It cites only traceable
  events (the `RUNS.md` self-check and restore, the 31-request probe, the 23-question
  real run, ADR-001/ADR-003); no quote is presented as verbatim.

---

## O. Improvement backlog

| Priority | Item | Why | Evidence needed |
|---|---|---|---|
| P0 | Add a real Groq key and run the probe | every real claim is blocked | `results/probe/*.json` |
| P0 | Calibrate the stand-in from real TTFT/latency | current stand-in is provisional | calibration table |
| P0 | Measure retrieval hit rate and answer quality | Table 3 is required | `results/retrieval_*.csv` |
| P1 | Tune evidence thresholds | false refusals or unsupported answers | eval confusion matrix |
| P1 | Add a neural embedding retriever (optional) | paraphrase recall is a known weakness | A/B retrieval hit rate |
| P1 | Add an LLM verifier A/B | deterministic checks are lexical, not entailment | token cost vs. quality |
| P2 | Add Redis/out-of-process cache | cache is per-process today | cache hit rate and restart loss |
| P2 | Add a semantic cache with a strict guard | potential cost win, high quality risk | false-positive suite |
| P2 | Add an HTTP API | not needed for the brief | latency overhead |
| P3 | Add p99 with confidence intervals | small samples make p99 noisy | more samples |

---

## P. Session checkpoints

### Session 1 — 2026-11-10

- **Completed:** Phase 0 audit; Phase 1 foundation; the full pipeline, providers,
  reliability, instrumentation, evaluation, experiments, tests, and documentation.
- **Tests:** recorded in section N and `CHANGELOG.md` after execution.
- **Problems:** no Groq key; no Redis/fastembed (deliberate); real results blocked.
- **Next:** run the real probe; calibrate; write the report with the owner.

---

*End of journal section created in session 1. Append new entries below this line; do not
rewrite history.*

### Session 2 — 2026-11-11

- **Completed:** real Groq probe (3× HTTP 200; limits confirmed from headers); first
  verified real answer (A01: 2,110 in / 59 out, TTFT 782 ms, total 875 ms); stand-in
  calibrated from 11 real calls (base_latency 810 ms, TTFT fraction 0.77,
  stream_chunk_ms 0); `.env.example` rewritten; README/CHANGELOG/LIVE_DISCUSSION_PREP
  added; test count raised from 84 to 89.
- **Fixed bugs:** BUG-002 (deadline ms/s mismatch), BUG-003 (zero-wait boundary),
  BUG-004 (double-counted in-flight reservations), BUG-005 (grader refusal-before-facts),
  BUG-006 (full-width citation brackets), BUG-007 (daily reset token events),
  BUG-008 (scheduler `max_queue_depth=0`), BUG-009 (retrieval_setting "none" for scope
  refusals). All have regression tests.
- **Changed:** removed unused `ResultsWriter` from `Relay`; runner/CLI owns one row per
  request; real quality deadline raised to 120 s for TPM pacing; Table 2a/2b exclude
  quality; Table 3 quality-only and source-separated; metrics CLI prints source.
- **Tests:** 89 pass (~3.5 s), no network/quota.
- **Stand-in canonical runs:** complete (105 rows: quality 69 + 5 load scenarios).
- **Real run v4:** in progress (`relay_real_balanced_v4`, 23 questions).
- **Problems:** Vaani feedback and collaboration history are owner-authored placeholders.
- **Next:** wait for v4; merge v4 rows via `scripts/merge_results.py`; `metrics --export`;
  `reconcile`; write `reports/REPORT.md`; commit with detailed messages.
