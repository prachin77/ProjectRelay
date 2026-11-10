# ASSIGNMENT REQUIREMENTS — The Relay

> **Authority note.** This document is derived from the *actual* assignment brief:
> `C:\Users\Prachin\Downloads\The_Relay_Assignment_Brief.pdf` (4 pages, 11,801 bytes,
> last modified 2026-11-09 17:43). Where the master prompt and the brief disagree,
> **the brief wins** and the disagreement is recorded in §5 (Conflicts).
>
> Status vocabulary: `Not started` · `In progress` · `Implemented` · `Tested` · `Verified` · `Blocked`
> A requirement is only `Verified` when there is an artifact (test output, log line,
> measurement, file) that proves it.

---

## 0. Assignment in one paragraph

Build a small working pipeline (Python/C++/Go) that answers engineers' questions from
exactly three arXiv papers, citing where in the papers each answer came from, and saying so
when the papers do not contain the answer. Use a real hosted model on Groq's free tier, find
its real limits empirically, and build a controllable stand-in model service so you can run
load shapes the free tier cannot survive. Then measure: latency distributions, failures,
token cost, and answer quality, in a fixed set of tables, driven by a fixed `results.csv`.
Write it up in **two pages** under **fixed headings**.

**Deadline:** 4 days from receipt. Brief dated 2026-11-09 → deadline ≈ **2026-11-13**.
Today is 2026-11-10. Remaining working time: ~3 days.

---

## 1. Verified environment facts (audited, not assumed)

| Fact | Value | Evidence |
|---|---|---|
| Working directory | `P:\ProjectRelay` | `Get-Location` |
| Pre-existing project files | **none** (empty directory, not a git repo) | `(Get-ChildItem -Force).Count` → `0`; `git status` → "not a git repository" |
| Python | 3.12.10 via `py` launcher | `py --version` |
| Python 3.11 also present | `C:\Program Files\Python311\python.exe` | `py -0p` |
| `python` / `pip` on PATH | **No** — must invoke via `py -m` | `where.exe python` → not found |
| Node.js | v24.12.0 | `node --version` |
| Docker CLI | 29.7.2, **daemon not running** | `docker info` → pipe not found |
| WSL2 | present, distro `docker-desktop`, **Stopped** | `wsl -l -v` |
| Native Redis / Memurai | **not installed** | `where.exe redis-server`; `Get-Service *redis*,*memurai*` |
| `GROQ_API_KEY` in shell | **not set** | `if ($env:GROQ_API_KEY)` |
| `pypdf` | installed 7.0.0 (during audit) | `py -c "import pypdf"` |

### 1.1 Corpus verification (exact versions, as mandated)

| Paper | arXiv ID | Version | Verified | Pages | Words (extracted) | SHA-256 (first 16) | Local file |
|---|---|---|---|---|---|---|---|
| Attention Is All You Need | 1706.03762 | **v7** (2023-08-02) | yes, `https://arxiv.org/abs/1706.03762v7` | 15 | 6,123 | `bdfaa68d8984f0dc` | `documents/attention_is_all_you_need_v7.pdf` |
| Moshi: a speech-text foundation model for real-time dialogue | 2410.00037 | **v1** (2024-09-17) | yes, `https://arxiv.org/abs/2410.00037v1` | 66 | 30,429 | `a687144cc77bea86` | `documents/moshi_v1.pdf` |
| Dynamic Chunking for End-to-End Hierarchical Sequence Modeling | 2507.07955 | **v2** (2025-07-15) | yes, `https://arxiv.org/abs/2507.07955v2` | 39 | 21,933 | `04a2b32e6e82853c` | `documents/dynamic_chunking_v2.pdf` |

Total corpus ≈ **120 pages / 58.5k words / ~76k model tokens**. Small enough that all
embeddings can be computed **locally and once**, with zero API cost.

**Extraction hazard found during the audit (this is real, and it matters):** the Moshi PDF
interleaves a running page header — *"Moshi: a speech-text foundation model for real-time
dialogue"* — into the extracted text of nearly every page. A naive heading detector picks it
up 106 times (e.g. `("2", "Moshi: a speech-text ...")`, `("3", "Moshi: a speech-text ...")`),
which corrupts section metadata and pollutes embeddings with repeated text. De-hyphenation and
header/footer stripping are therefore **required**, not optional polish.

---

## 2. Requirement matrix — explicit brief requirements

| # | Requirement | Source in assignment | Implementation plan | Test method | Status |
|---|---|---|---|---|---|
| R1 | Implementation language is Python, C++ or Go | p1 "What to build" | Python 3.12 | `py --version`; repo layout | Verified (env) |
| R2 | Pipeline of **at least four steps** | p1 | 6 steps: normalise → scope → cache → retrieve(tool) → evidence → generate → validate | `tests/test_pipeline_steps.py` asserts ordered stage list | Not started |
| R3 | **At least one tool call** | p1 | `retrieve_passages` is a registered tool with a typed interface, invoked by the orchestrator and logged in `model_calls.jsonl`/`request_traces.jsonl` | trace contains `stage=tool_call, tool=retrieve_passages` | Not started |
| R4 | **At least one branch or loop** | p1 | Branch: scope filter / cache hit / evidence outcome. Loop: bounded re-retrieval (≤2 extra attempts) when evidence is insufficient | `tests/test_loop.py` forces insufficient evidence and asserts ≤2 loops | Not started |
| R5 | One step **retrieves passages from the library** | p1 | Hybrid BM25 + dense retrieval over the 3 papers | retrieval unit test on known passage | Not started |
| R6 | Library = exactly these 3 papers, **at exactly these versions** | p1 | Pinned PDFs in `documents/`, SHA-256 in corpus manifest | §1.1 hashes; `scripts/verify_corpus.py` | **Verified** (fetched, hashed, parsed) |
| R7 | **Fetch them yourself** | p1 | Downloaded from arXiv with version-qualified URLs | `documents/*.pdf` exist with hashes | **Verified** |
| R8 | Every answer must **cite where in the papers** it came from | p1 | Citations carry paper + page + section + chunk id | `tests/test_citations.py`; eval citation score | Not started |
| R9 | If the papers do not say, **say so** instead of guessing | p1 | Evidence sufficiency gate → explicit refusal text, zero LLM call when deterministically refusable | eval questions designed to be unanswerable; `answer_grade=refused_correctly` | Not started |
| R10 | Real hosted model on **Groq free tier**, own free account + key | p1 | `providers/groq.py` adapter, key from `GROQ_API_KEY` | one real call returns 200 + usage | **Blocked** — no key supplied yet |
| R11 | Find real limits from **documentation and from what it returns** | p1 | `experiments/probe.py` records headers on success and on 429 | Table 1 filled from probe output | Not started |
| R12 | Write a **stand-in model service you control** | p1 | `providers/standin.py` | `tests/test_standin.py` | Not started |
| R13 | Stand-in can be **slow, fail, refuse on limit, slower under load, stream piece by piece** | p1 | Explicit knobs: `base_latency_ms`, `failure_rate`, `rate_limit_at`, `load_slope`, `stream_chunk_ms` | one test per behaviour | Not started |
| R14 | Calibrate stand-in from real measurements; **say where it is least like the real thing** | p1 | `config/standin_calibration.json` derived from `results/probe/` | calibration table in report §2 + journal | Not started |
| R15 | First send a **modest number of real requests** to learn latency spread, limits, behaviour at limit | p1 | Probe run: 30 real requests, recorded in `results/probe/` | `results.csv` rows `scenario=probe` | Not started |
| R16 | Run pipeline under **at least three load shapes** of own design | p1 | steady / burst / sustained overload (+ 2 required by Table 2a) | `results/*.csv` per scenario | Not started |
| R17 | Use the stand-in for loads the real service cannot take | p1 | Stand-in runs for overload and budget scenarios | `source=stand-in` rows | Not started |
| R18 | **Keep the raw results for both** | p1 | `results/` retains per-run JSONL + CSV | files exist and are referenced | Not started |
| R19 | Maximum **2 pages of writing** | p2 / p4 | `reports/REPORT.md` constrained to 2 pages of dense tables+prose | page count check | Not started |
| R20 | Headings **in this order**, a page that skips one is incomplete | p2 | exact heading list in `reports/REPORT.md` | heading-lint script | Not started |
| R21 | **Two extra headings after heading 5**: "Retrieval: quality versus cost"; "How this library will mislead a naive retriever" | p2 | inserted at positions 6 and 7 | heading-lint | Not started |
| R22 | One more heading: "Your feedback on Vaani" | p2 | visit vaani.dev, write honest few lines | section present, authored by user | **Blocked** — needs user's own opinion |
| R23 | Finish with "How I worked with the assistant" | p2 | includes real conversation history/prompts | section present | Not started |
| R24 | **Table 1** limits: RPM, TPM, RPD, TPD, in-flight, what it returns at limit, recovery time — each with how found + confidence | p3 | filled from probe + docs | every cell non-empty or `not measured` | Not started |
| R25 | **Table 2a** latency per scenario, ≥ steady, burst, sustained overload, per-minute limit halved, daily budget exhausted | p3 | 5 scenarios minimum | one row per scenario | Not started |
| R26 | **Table 2b** outcomes, same rows: success %, rate-limited %, timeout/given-up %, retries/request, peak queue, tokens/answer, cost/1000 answers | p3 | computed by `eval/metrics.py` from `results.csv` | recompute-check script | Not started |
| R27 | **Table 3**: ≥3 retrieval settings varying (piece size, pieces fetched, re-rank) with correct+cited %, wrong %, correctly refused %, tokens/answer, TTFT p50/p95, total p50/p95 | p3 | 3 configs: `light`, `balanced`, `extensive` | re-index + eval per config | Not started |
| R28 | `results.csv` with the **exact 13 columns**, one row per request | p4 | `obs/results_csv.py` with frozen header | header equality test | Not started |
| R29 | Every figure in the tables must be **computable from `results.csv`** | p4 | all metrics derived from that file only | `scripts/recompute_tables.py` reproduces tables | Not started |
| R30 | Submit within 4 days | p4 | deadline ≈ 2026-11-13 | — | In progress |
| R31 | ≤2 pages writing + code + raw results + conversation history | p4 | deliverables list | — | Not started |
| R32 | Own free account; **do not include key**; daily limit is shared so plan requests | p4 | `.env`, `.env.example`, `.gitignore`; no key in output | `grep` for key pattern in repo | Not started |
| R33 | Stay within provider terms; no extra accounts or tricks | p4 | single key, no rotation, no sharding | code review | Not started |
| R34 | AI assistant welcome; judged on direction | p4 | conversation log preserved verbatim | `docs/conversation/` | In progress |
| R35 | Live conversation follows; only submit what is defensible | p4 | `LIVE_DISCUSSION_PREP.md` + change exercises | — | Not started |
| R36 | Answer quality on **≥15 self-written questions** | p2 | `eval/dataset.py` — 20 questions across categories | grader output | Not started |
| R37 | **Tokens used per answer** | p2 | per-request `tokens_in`/`tokens_out` aggregated across stages+retries | one CSV row per request | Not started |
| R38 | Latency of one slow request split into **waiting / working / retrying**, before or after first token, show how you know | p2 | stage timestamps in `request_traces.jsonl` | worked example in report §4 | Not started |
| R39 | Behaviour at **per-minute** and **per-day** limit: who delayed, dropped, gets weaker answer | p2 | admission control + degraded retrieval tier | scenarios `rpm_half`, `daily_exhausted` | Not started |
| R40 | Retry policy + **one case where retrying made things worse** | p2 | bounded retries with jitter; find a real harmful retry in traces | trace evidence quoted | Not started |
| R41 | What a waiting person sees at 3pm when daily budget is gone | p2 | honest static message, no invented ETA | message snapshot test | Not started |
| R42 | Least certain assumption + what breaks if wrong | p2 | journal-sourced | section present | Not started |
| R43 | Two things deliberately not built | p2 | journal §"deliberately not built" | section present | Not started |
| R44 | One thing new, in own words | p2 | user-authored | section present | Not started |
| R45 | **Three ways this library misleads a naive retriever**, each with example question | p2 | derived from measured failures | examples reproducible against the index | Not started |
| R46 | Three prompts that mattered most; one thing the assistant got wrong that you caught; one decision you overruled | p2 | user-authored from real history | section present | **Blocked** — needs user |
| R47 | What is distinctly yours | p2 | user-authored | section present | **Blocked** — needs user |
| R48 | Include **real** conversation history, or prompts if tool cannot export | p2 | export this session's prompts verbatim | `docs/conversation/` | In progress |
| R49 | Include **a number we could not have guessed without running it** | p2 | candidate: TPM binds before RPM; measured cold-start TTFT | measured value quoted | Not started |
| R50 | Say which runs were real and which used the stand-in | p2 | `source` column in every row and table | every table row labelled | Not started |
| R51 | Say which percentiles and how many requests | p2 | p50/p95/p99 + n per row | table columns filled | Not started |
| R52 | Milliseconds for time, one decimal for percentages | p3 | formatter enforced | format test | Not started |
| R53 | Write "not measured" where not measured | p3 | no blank cells | lint | Not started |
| R54 | Choose one retrieval setting, say why, and say what you do with retrieval when the limit is close | p2 | chosen config justified by Table 3 + degraded tier | report §6 | Not started |

---

## 3. Requirements inherited from the master prompt (NOT in the brief)

These are build requirements **I am imposing on myself on the user's instruction**. They are
not graded by the brief, and the report must not pretend they were. They are listed here so
the distinction stays honest.

| # | Requirement | Origin | Why keep it | Status |
|---|---|---|---|---|
| A1 | Redis-backed **exact** answer cache | master prompt §5 | With TPM = 8,000 tokens/min, a repeated question costs a full minute of budget. This is the single highest-leverage cost fix. | Not started |
| A2 | Redis-backed **semantic** cache with a conservative equivalence guard | master prompt §5.4–5.5 | Same reason; but must never trade correctness for hit rate | Not started |
| A3 | Version-aware cache keys, TTL, invalidation, fail-open on Redis outage | master prompt §5.9–5.11 | Prevents stale answers surviving a corpus/prompt/model change | Not started |
| A4 | Central quota manager + admission control + bounded queue + deadlines | master prompt §7 | Required to answer report headings 5 and 7 honestly | Not started |
| A5 | `request_traces.jsonl`, `model_calls.jsonl`, cache trace fields | master prompt §12 | Required to answer heading 4 (waiting/working/retrying) | Not started |
| A6 | `PROJECT_JOURNAL.md`, `IMPLEMENTATION_PLAN.md`, `LIVE_DISCUSSION_PREP.md` | master prompt §13, §18.4 | Study + defence artifact | In progress |
| A7 | Automated test suite + real Redis integration test | master prompt §16 | "Verified" must mean measured | Not started |
| A8 | Beginner-friendly explanatory comments | master prompt §14 | User must be able to defend the code live | Not started |

---

## 4. What is *not* required (recorded so we do not over-build)

The brief explicitly says: *"You do not need to cover every technique. Pick the few that
matter for your measurements and go deep."*

- No web UI required. A CLI + thin HTTP endpoint is enough.
- No multi-agent architecture required. The brief's framing ("chaining several model calls")
  is the *problem*, not a requirement to reproduce.
- No vector database server required. 120 pages fits in process memory.
- No authentication/multi-tenant concerns.
- No deployment/containers required.
- Streaming is required **only** for the stand-in ("send its reply a piece at a time").

---

## 5. Conflicts between the master prompt and the actual brief

| # | Conflict | Brief says | Master prompt says | Decision | Rationale |
|---|---|---|---|---|---|
| C1 | Redis | says nothing about caching | "mandatory architectural component" | **Build Redis, but label it as our own design decision**, not a graded requirement | It is genuinely the right call under TPM=8k; but the report must not claim the brief asked for it |
| C2 | "Three retrieval configurations" | Table 3: three *settings* defined by piece size, pieces fetched, re-rank | light / balanced / extensive tiers | **One set of three**, named `light`/`balanced`/`extensive`, each defined by (chunk size, top-k, rerank) | Satisfies both with one measurement effort |
| C3 | Answer verification | heading 1 mentions the pipeline "checks the reply" | a conditional second LLM call | **Deterministic grounding + citation validation by default**; LLM verifier behind a flag, measured, default off | On 8k TPM a verifier call ~doubles token cost and halves throughput; must be proven to be worth it |
| C4 | Report structure | 8 fixed headings + 3 inserted ones | 11 generic sections + generic 2-page report | **Brief wins** | "A page that skips one is incomplete" |
| C5 | Evaluation set size | ≥15 questions | ≥15 questions | agree | none |
| C6 | Vaani feedback | required | required | user must author; I will scaffold and research the site | Do not fabricate opinions |
| C7 | Conversation history | required, "real, not cleaned-up" | required, "do not fabricate" | export verbatim from this session | Integrity |

---

## 6. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **No Groq key** → no real measurements → whole report unverifiable | high (currently absent) | fatal | Must be supplied before Phase 5. Everything else can proceed. |
| TPD = 200,000/day cannot cover probe + 20-question eval × 3 settings + 3 load shapes | high | high | Budget explicitly; split across days; stand-in for load shapes; measure only what the report needs |
| 2-page limit is extremely tight for 4 tables + 8 headings | high | high | Write tables first from `results.csv`, prose second, hard page budget |
| TPM = 8k means ~7 requests/min regardless of RPM = 30 | high | medium | That *is* the finding; design admission control around tokens, not requests |
| Stand-in "least like the real thing" must be honest and specific | medium | medium | Calibrate p50/p99 and error mix; document what it cannot reproduce (real model variance) |
| Redis not running (no daemon, no native install) | high | medium | Fail-open design; Docker for integration tests; in-memory fake for unit tests |
| `sentence-transformers`/torch is a ~2.5 GB install on Windows | medium | medium | Prefer ONNX `fastembed` (no torch) |
| Extraction artifacts (Moshi running headers) corrupt metadata | confirmed | medium | Header/footer stripping + de-hyphenation, verified by test |

---

## 7. Deliverable checklist mapped to files

| Deliverable | File |
|---|---|
| Source code | `the_relay/` |
| Three retrieval settings | `the_relay/rag/retrieval.py`, `config/retrieval.yaml` |
| ≥15 questions | `the_relay/eval/dataset.py` + `eval/questions.jsonl` |
| Groq integration | `the_relay/providers/groq.py` |
| Stand-in | `the_relay/providers/standin.py` |
| Load scenarios | `the_relay/experiments/scenarios.py` |
| `results.csv` | `results/results.csv` |
| Request traces | `logs/request_traces.jsonl` |
| Model-call traces | `logs/model_calls.jsonl` |
| Cache traces | `logs/cache_traces.jsonl` |
| Two-page report | `reports/REPORT.md` (+ PDF/DOCX for submission) |
| Vaani feedback | `reports/REPORT.md` §"Your feedback on Vaani" (user-authored) |
| Conversation history | `docs/conversation/` |
| Live prep | `LIVE_DISCUSSION_PREP.md` |
| Journal | `PROJECT_JOURNAL.md` |
| Requirements | `ASSIGNMENT_REQUIREMENTS.md` (this file) |
| Plan | `IMPLEMENTATION_PLAN.md` |

---

## 8. Open questions for the user

1. **Groq API key** — required before Phase 5. Where will it come from?
2. **Redis** — Docker container, Memurai service, or in-process only for now?
3. **Model choice** on free tier: `openai/gpt-oss-120b` (best quality, 500 t/s) vs
   `openai/gpt-oss-20b` (1000 t/s, same free limits). Recommend `gpt-oss-120b` for quality,
   measure both if budget allows.
4. **Vaani feedback** — you must author this after visiting the site.
5. **Heading 8 items** (R42–R44, R46–R47) require *your* judgement and must not be invented.
