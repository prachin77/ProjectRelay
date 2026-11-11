# The Relay

A small, evidence-grounded research assistant over exactly three arXiv papers:

| Key | Paper | arXiv version |
|---|---|---|
| `attention` | *Attention Is All You Need* | 1706.03762v7 |
| `moshi` | *Moshi: a speech-text foundation model for real-time dialogue* | 2410.00037v1 |
| `dynamic_chunking` | *Dynamic Chunking for End-to-End Hierarchical Sequence Modeling* | 2507.07955v2 |

The assistant answers **only** from these PDFs, cites the paper, page and
section for every factual sentence, and refuses when the retrieved evidence does
not support an answer. The project also measures its own reliability and cost:
every user request produces one row in a strict 13-column `results.csv`.

---

## Quick start (no API key required)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Build the three indexes (light / balanced / extensive)
python -m the_relay.main ingest

# Ask a question with the deterministic stand-in
python -m the_relay.main ask "What BLEU score did the big Transformer achieve on WMT 2014?"

# Run the automated tests
python -m pytest
```

The stand-in is **not** an LLM. It is a deterministic extractive baseline that
selects sentences from the retrieved evidence and cites them. It exists so the
whole pipeline, including failures and load behaviour, can be exercised without
spending quota. Every row it produces is labelled `source=stand-in`.

## Using the real Groq provider

1. Copy `.env.example` to `.env`.
2. Set `GROQ_API_KEY` and `RELAY_PROVIDER=groq`.
3. Probe the account before running anything expensive:

```powershell
python -m the_relay.main --provider groq probe --requests 3
```

The probe records HTTP status, latency and the real `x-ratelimit-*` headers in
`logs/probe.jsonl`. Those headers are the authoritative limits for the account,
not the documentation.

```powershell
python -m the_relay.main --provider groq ask --preset balanced "Your question"
```

---

## What happens to one question

```
question
  -> scope check                  (is it about these papers?)
  -> exact cache                  (safe: key includes corpus + prompt + model)
  -> paper targeting              (does the wording name one paper?)
  -> retrieval                    (BM25 + TF-IDF + RRF + deterministic reranker)
  -> evidence gate                (sufficient / partial / insufficient)
       insufficient -> refuse, zero model calls
  -> bounded evidence loop        (at most `evidence_max_loops` extra retrievals)
  -> quota admission              (token-aware rolling minute/day windows)
  -> scheduler                    (concurrency cap, bounded queue, deadline)
  -> provider call                (Groq streaming, or the stand-in)
  -> verification                 (citations exist and are supported)
       failure -> one regeneration -> transparent partial answer if needed
  -> one row in logs/results.csv
```

Two details matter for honesty:

* **Refusal is a feature.** If the named paper's body never mentions a
  distinctive term in the question (references excluded), the system refuses
  instead of answering from a topically adjacent passage.
* **Estimates are labelled.** Before a call, token counts are estimated. After
  a successful call, the provider's reported usage replaces the estimate in
  `logs/model_calls.jsonl` and the row. Failed calls are charged conservatively
  in traces and marked `usage_estimated=true`.

---

## Commands

| Command | What it does |
|---|---|
| `ingest` | Extract and index the three PDFs at all three retrieval presets |
| `ask "..."` | Answer one question, optionally `--stream` |
| `probe --requests N` | Send N real requests and record limits/latency |
| `eval --presets light balanced extensive` | Answer the 23-question evaluation set |
| `experiment --scenarios steady burst overload reduced_quota daily_exhausted` | Run the five load shapes |
| `metrics [--json] [--export]` | Compute Tables 2a, 2b and 3 from `results.csv` |
| `reconcile` | Check that model-call logs sum to the CSV rows |

Useful flags:

```powershell
--provider groq|standin      # override RELAY_PROVIDER for one command
--preset light|balanced|extensive
--deadline-ms 25000
```

---

## Evaluation

`evaluation/questions.json` contains 23 hand-written questions: 19 answerable
and 4 deliberately unanswerable controls. Every answerable question lists the
fact groups that a correct answer must contain, so grading is auditable rather
than a model judging another model.

Grades are exactly the four values from the brief:

| Grade | Meaning |
|---|---|
| `correct` | All required facts are present **and** the answer cites at least one source |
| `wrong` | A fact is missing, the citation is missing, or an infrastructure failure delivered no answer to an answerable question |
| `refused_correctly` | The question is unanswerable and the assistant refused |
| `refused_wrongly` | The question is answerable but the assistant refused |

Infrastructure failures are graded `wrong` for answerable questions and
`refused_correctly` for unanswerable ones. That rule is stated in the report so
quality percentages can be read correctly.

---

## Repository layout

```
the_relay/
  config.py            all settings, with the unit contract documented
  core/                scope, evidence, quota, scheduler, retry, verification, pipeline
  rag/                 ingest, index, retrieval, paper targeting
  providers/           Groq adapter, deterministic stand-in, factory
  eval/                dataset loader, grader, metrics
  experiments/         scenarios, budget planner, runner
  obs/                 traces, model-call log, strict results.csv writer
config/                retrieval presets, stand-in calibration, corpus manifest
documents/             the three source PDFs
evaluation/            questions.json
tests/                 89 automated tests (no network, no quota)
reports/               two-page report and live-discussion preparation
logs/                  raw traces and the canonical results.csv (git-ignored, regenerable)
results/               exported evidence and probe summaries
```

---

## Tests

```powershell
python -m pytest
```

The suite covers paper targeting, corpus-absence refusal, the evidence gate,
quota admission (including the rolling-window boundary), the bounded scheduler,
retry classification, exact-cache keys, citation normalisation and verification,
the strict CSV schema, stand-in determinism, and end-to-end pipeline runs. It
never calls Groq.

---

## Results journey (what changed and why)

The project was built incrementally. Each change below moved a measured number;
the previous value is kept so the progression is visible.

| Change | Why | Before | After |
|---|---|---|---|
| Grader: refusal checked before facts | An answer with all facts plus an honest caveat was graded `refused_wrongly` | A03, A04, M01, D01 graded `refused_wrongly` despite containing required facts | Facts decide the grade first; refusal only marks `refused_wrongly` when facts are missing |
| Grader: unicode hyphen normalisation | Groq emits non-breaking hyphens (U+2011); `Encoder-decoder` failed to match `encoder-decoder` | Real run: 12 correct, 7 refused_wrongly, 4 refused_correctly | Re-grade: 14 correct, 5 refused_wrongly, 4 refused_correctly (A04, M01 flipped) |
| Stand-in calibration (base_latency 850→810, ttft 0.35→0.77, stream_chunk 20→0) | Provisional values pre-dated any real measurement | Stand-in TTFT ~300 ms (unrealistic) | Stand-in TTFT ~620 ms (matches real median 703 ms) |
| Deadline unit fix (ms→s) | `deadline_mono` was in ms but compared to `time.monotonic()` in s | 4 real requests rejected as `rate_limited` despite having quota | All 23 real questions completed with `status=ok` |
| Zero-wait boundary fix | `wait_s==0` at rolling-window boundary raised instead of re-checking | 4 admissible requests rejected | Re-check succeeds |
| Scheduler `max_queue_depth=0` fix | `_waiting >= 0` rejected the first request even with a free worker | `test_full_queue_is_rejected_immediately` failed | Test passes; first request admitted |
| Out-of-scope `retrieval_setting` fix | Scope refusals wrote `"none"`, creating a phantom Table 3 row | Table 3 had 4 settings (light/balanced/extensive/none) | Table 3 has 3 settings; U01 counted under its preset |
| Real quality deadline 60s→120s | TPM pacing: 4th call waits ~58 s; 60 s deadline expired during normal pacing | 1 timeout in v2 run | 0 timeouts in v4 run (724.1 s wall clock) |

The raw measurements (128 rows) are in `logs/results.csv` and exported to `results.csv`.
Every table in `reports/REPORT.md` is recomputable from that file via `metrics --export`.

## Honest limitations

- Retrieval is lexical (BM25 + TF-IDF), not neural. Paraphrased questions can
  miss a passage that a human would find.
- The evidence gate is a heuristic, not entailment. It can produce a false
  refusal or accept a passage that merely shares vocabulary.
- The deterministic verifier checks citation existence and lexical support. It
  cannot prove that a cited sentence entails a claim.
- The stand-in is an extractive baseline. Its quality numbers describe the
  pipeline, not a language model.
- The exact cache is in-process, so it is lost on restart and not shared
  between workers.
- Real Groq quality latencies include admission waiting for the shared TPM
  window; they are not directly comparable to stand-in latencies.

See `PROJECT_JOURNAL.md` for the full engineering record, including failed
experiments and the reasoning behind each design decision.
