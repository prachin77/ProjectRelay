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

1. **Low cost** — minimise model calls, input/output tokens, redundant retrieval and
   verification, and retry waste.
2. **Quality** — evidence-grounded answers, accurate citations, correct refusal, no
   unsupported claims.
3. **Speed** — TTFT, total latency, queue wait, stage timings.
4. **Availability** — honest communication, never fabricated answers to stay up.

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

- Our account's exact limits: **[UNKNOWN]** until a key is supplied and the probe runs.
- Whether our account has separate ITPM/OTPM limits: **[UNKNOWN]**; the docs say some
  organisations do and the limits page shows an "X in / Y out" breakdown.
- In-flight concurrency limit: **[UNKNOWN]**; the docs do not publish one. Our application
  cap is a design choice, not a provider limit.
- Recovery time after a 429: **[UNKNOWN]** until measured.
- The exact 429 body shape for our account: [DOC] describes `429 Too Many Requests`; body
  details are recorded by the probe when available.

---

## H. Real Groq experiment history

| Experiment | Date | Model | Requests | Rate | Concurrency | Result |
|---|---|---|---|---|---|---|
| Probe | — | — | — | — | — | **Blocked: no API key** |

No real request has been sent. Nothing in this journal claims otherwise.

---

## I. Stand-in calibration history

The stand-in's parameters are **provisional engineering settings**, not calibration to
Groq. No real observations exist to calibrate against. The file
`config/standin_calibration.json` is labelled accordingly.

- **Real measurements used:** none.
- **Simulated parameters:** see `config/standin_calibration.json`.
- **Calibration method:** none yet; values chosen to produce plausible order-of-magnitude
  behaviour and to exercise the application's policies.
- **Random seed:** 42 by default.
- **Known inaccuracies:** no semantic variance, no real tokenizer, no provider-side
  queueing, no real model quality, no exact Groq latency distribution.
- **Update rule:** once real probe data exists, replace the provisional latency and failure
  parameters and record the divergence table here.

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
- **Outcome:** pending test run.
- **Remaining risk:** a legitimate repeated sentence could be removed if it appears on
  ≥30% of pages. The detector only removes short lines (<120 chars) that repeat exactly.

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
decision must be authored by the owner from the real conversation. A scaffold is in
`reports/REPORT.md` and `LIVE_DISCUSSION_PREP.md`; it is not filled with invented history.

---

## N. Current system state

### N.1 At the end of session 1

- Architecture: as in C.2.
- Configuration: as in F.
- Working: corpus verification, ingestion, retrieval, scope/evidence gates, generation
  (stand-in), deterministic verification, quota, scheduler, retry, instrumentation,
  evaluation harness, experiments (stand-in), tests.
- Blocked: real Groq probe and real load runs (no key); Vaani feedback (owner).
- Known bugs: see L; add new ones as found.
- Latest test results: see the session checkpoint in the conversation and `CHANGELOG.md`.
- Latest experiment results: see `results/` and section J.
- Remaining quota: **unknown** (no account key).
- Outstanding tasks: run real probe; calibrate stand-in; run real steady/burst if budget
  allows; write Vaani feedback; author collaboration history; generate final two-page
  report; run the live-discussion modification exercises.

### N.2 Recommended next steps (ordered)

1. Add `GROQ_API_KEY` to `.env` and run `py -m the_relay.main probe --requests 10`.
2. Use the probe output to replace provisional stand-in latency/failure parameters.
3. Run the real evaluation on the balanced preset (20 questions) if budget allows.
4. Re-run the stand-in load scenarios with the calibrated stand-in.
5. Write the two-page report from the generated tables; verify every number reconciles.
6. Author Vaani feedback and the collaboration section.
7. Practise the eight modification exercises in `LIVE_DISCUSSION_PREP.md`.

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
