# Changelog

Newest entries first. This file records **successes and failures** because the
assignment asks for an engineering record, not a highlight reel. Every entry is
something that was observed or changed in this repository.

---

## 2026-11-11 — Real provider, grading, and reliability fixes

### Fixed — real quality runs rejected admissible requests

Two independent bugs combined to make four real Groq quality requests fail with
`rate_limited` even though the account had quota:

1. **Unit mismatch.** `pipeline.ask` built `deadline_mono` in milliseconds while
   `QuotaManager.acquire` and `BoundedScheduler` compare against
   `time.monotonic()` in seconds. The logged deadline was ~90,096,986 seconds.
2. **Boundary rejection.** At the exact 60-second rolling-window boundary,
   `try_acquire` returned `wait_s == 0.0`; `acquire` treated `wait_s <= 0` as a
   hard failure instead of re-checking milliseconds later.

Fixed by converting the deadline to seconds explicitly and by retrying the
zero-wait boundary. Also removed a double count of in-flight reservations
(`_token_events` already includes them). Added regression tests
(`tests/test_quota.py`).

### Fixed — real quality runs timed out during normal TPM pacing

A shared 8,000 TPM window admits about three ~2.2k-token calls per minute. The
fourth must wait ~58 s. The real quality harness used a 60 s deadline, so normal
pacing produced a `timeout` row. Real quality runs now use 120 s. The report
states that real quality latency includes admission waiting.

### Fixed — grader marked correct answers as refusals

`eval/grader.py` checked refusal phrases before required facts. An answer such
as “the BLEU score was 28.4 [S1]. The papers do not provide a per-language
breakdown.” was graded `refused_wrongly`. Facts now decide an answerable
question's grade; a refusal only marks `refused_wrongly` when required facts are
missing. Added regression tests.

### Fixed — full-width citation brackets caused a false refusal

Groq emitted `【S1】` in one live run. The verifier only recognised ASCII
`[S1]`, so a correct answer was discarded. `normalise_citation_brackets` now maps
U+3010/U+3011 and full-width square brackets to `[` and `]` before verification.

### Fixed — `gpt-oss-120b` returned empty completions

The model is a reasoning model. With a small `max_tokens`, hidden reasoning
consumed the budget and the visible text was empty. Added
`RELAY_GROQ_REASONING_EFFORT=low`.

### Fixed — scheduler rejected the first request in no-queue mode

`BoundedScheduler` with `max_queue_depth=0` evaluated `_waiting >= 0` and
rejected even when a worker was free. The test suite exposed it; the check now
only applies when the queue depth is positive.

### Fixed — daily reset returned zero when only tokens were preconsumed

`_day_reset_s` looked only at request timestamps, so the `daily_exhausted`
scenario reported “next capacity in 0.0s”. Reset calculations now consider token
events as well. Added a regression test.

### Added — 84 automated tests

`tests/` covers targeting, corpus-absence refusal, the evidence gate, quota
admission, scheduling, retries, cache keys, citation normalisation, verification,
the CSV schema, stand-in determinism and end-to-end runs. No test calls Groq.

### Added — deterministic paper targeting

`rag/targeting.py` restricts retrieval when a question names one paper, and
detects distinctive question terms that never occur in that paper's body. This
fixed three unanswerable controls (ImageNet, video, speech) that previously
received topically adjacent answers.

### Changed — removed an unused `ResultsWriter` from `Relay`

The runner owns row writing and grading. Keeping a second writer in the pipeline
invited duplicate rows for one request. The pipeline now documents that the
harness writes exactly one row.

---

## 2026-11-11 — Real Groq probe and first verified answer

### Succeeded

* `probe --requests 3` returned HTTP 200 three times. Observed headers:
  `x-ratelimit-limit-requests: 1000`, `x-ratelimit-limit-tokens: 8000`;
  remaining tokens fell 7906 → 7898 → 7849, remaining requests 999 → 997.
* One `ask` for A01 returned a verified, cited answer:
  “The Transformer’s large (“big”) model achieved a BLEU score of 28.4 on the
  WMT 2014 English-to-German translation task[S1].”
  Measured 2,110 input / 59 output tokens, 782 ms to first token, 875 ms total.

### Failed / observed

* The probe requested `max_tokens=8`, and `gpt-oss-120b` returned empty text
  because reasoning consumed the budget. Probe latency is still valid; the text
  is not. Documented rather than hidden.
* Probe `first_token_ms` is `null` because the probe call is non-streaming.
  Streaming TTFT comes from `ask`, not `probe`.

---

## 2026-11-11 — Stand-in retrieval quality

### Succeeded

* Balanced preset reached **13/19 answerable correct**, 6 wrong, 4/4
  unanswerable refused correctly, with all 23 requests `status=ok`.
* The remaining wrong answers are extractive stand-in misses (it quotes the
  wrong sentence), not fabricated facts.

### Failed then fixed

* A greedy section-heading regex deleted the subject of sentences such as
  `5.3 Optimizer We used the Adam optimizer [20] ...`, leaving `[20] with ...`.
  Replaced with heading removal driven by the citation label.
* Table rows flattened into prose (`N d model dff h d k dv ... 6 512 2048 ...`)
  crowded out real sentences. Added a numeric-density filter.
* Bibliography rows were treated as evidence. Added boilerplate/back-matter
  filters.
* Numeric questions selected generic sentences over the sentence containing the
  number. Added a numeric-answer boost.

---

## 2026-11-10 — Foundation

* Extracted the assignment brief, verified PDF SHA-256 hashes and page counts.
* Built ingestion, three retrieval presets, BM25 + TF-IDF + RRF, deterministic
  reranking, evidence gate, scope gate, quota manager, bounded scheduler,
  retry policy, exact cache, Groq adapter, deterministic stand-in, traces,
  model-call log, strict CSV writer, evaluation harness and load scenarios.
* Decided against Redis, fastembed, FastAPI and a semantic cache; the reasons
  are recorded in `PROJECT_JOURNAL.md` (ADR-006 .. ADR-009).
