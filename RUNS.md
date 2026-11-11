# Tests and Metrics — Hyper-Nuanced Breakdown

This document provides the exact function, file, math, and motive for every reported
figure and every automated test. It complements `RUNS.md` (which records what was
run) by explaining *how* each number is computed and *what* each test proves.

---

## Part 1 — How every figure is calculated

All figures are computed by `the_relay/eval/metrics.py` from `logs/results.csv`
(read by `the_relay/obs/results_csv.py::read_results`). The CSV has 12 columns:
`request_id, scenario, source, start_time, first_token_ms, total_ms, status,
retries, tokens_in, tokens_out, retrieval_setting, answer_grade`.

### Helper functions (the_relay/eval/metrics.py)

| Function | Line | What it does |
|---|---|---|
| `_number(cell)` | 42 | Parses a CSV cell to float. Empty/None → None (missing, not zero). |
| `percentile(values, p)` | 52 | Nearest-rank percentile: `ordered[ceil(p/100 * n) - 1]`. No interpolation. |
| `_percentile_triplet(values)` | 63 | Returns `{"p50": ..., "p95": ..., "p99": ...}`. |
| `_parse_time(value)` | 71 | Parses ISO-8601 timestamp to datetime. |
| `_duration_seconds(rows)` | 81 | Wall-clock span: `max(end) - min(start)` where end = start + total_ms/1000. |
| `_peak_concurrency(rows)` | 96 | Event-sweep: +1 at start, -1 at end, sort ends before starts at same timestamp. Returns peak. |
| `table_2a(rows)` | 127 | Groups by scenario (excludes quality), computes duration, offered rate, TTFT/total percentiles. |
| `table_2b(rows)` | 171 | Groups by scenario (excludes quality), computes status %, retries, peak queue, tokens/answer, cost. |
| `table_3(rows)` | 239 | Groups by (retrieval_setting, source) for quality rows only. Computes grade %, tokens, TTFT percentiles. |
| `grade_counts(rows)` | 307 | Counts of each grade value across all rows. |

### Figure-by-figure breakdown

#### TTFT p50 / p95 / p99
- **Function:** `percentile(values, p)` in `metrics.py:52`
- **Source column:** `first_token_ms`
- **Math:** Nearest-rank. Sort values ascending. Index = `ceil(p/100 * n) - 1`. Return `sorted[index]`. No interpolation.
- **Missing handling:** Rows with empty `first_token_ms` (refused, rate-limited, timed out) are **excluded** from the list, not treated as 0. If no values, returns None.
- **Example (steady, n=6):** first_token_ms = [782, 797, 640, 656, 641, 657]. Sorted: [640, 641, 656, 657, 782, 797]. p50 index = ceil(0.5*6)-1 = 2 → 656. p95 index = ceil(0.95*6)-1 = 5 → 797. p99 index = ceil(0.99*6)-1 = 5 → 797.
- **Note:** With n=6, p95 and p99 often equal the max. This is by design — "an interpolated p99 from 12 requests would look more precise than the data allows."

#### Total time p50 / p95 / p99
- **Function:** `percentile(values, p)` in `metrics.py:52`
- **Source column:** `total_ms`
- **Math:** Same nearest-rank as above. All rows included (even rejected ones with total_ms=0 or small).
- **Missing handling:** `total_ms` is never empty in practice (every request has a duration). `_number` returns None only for empty strings.

#### Duration (s)
- **Function:** `_duration_seconds(rows)` in `metrics.py:81`
- **Source columns:** `start_time` + `total_ms`
- **Math:** For each row: parse `start_time` to datetime, compute `start_s = timestamp()`, `end_s = start_s + total_ms/1000`. Duration = `max(end_s) - min(start_s)` across all rows in the group.
- **Why not sum of totals:** Because requests overlap in time (concurrent). Duration is wall-clock span, not sum of individual durations.
- **Example (steady):** 6 requests, 20 s apart, each ~1.4 s. First starts at T=0, last ends at T=100+1.4=101.4 s. Duration ≈ 101.7 s.

#### Offered rate (per min)
- **Function:** `table_2a()` in `metrics.py:159`
- **Math:** `requests_sent / (duration_s / 60.0)`, rounded to 1 decimal.
- **Example (steady):** 6 / (101.7/60) = 6/1.695 = 3.54 → 3.5/min.

#### Succeeded % / Rate-limited % / Timed out or given up %
- **Function:** `table_2b()` in `metrics.py:212-221`
- **Source column:** `status`
- **Math:** `count(status=X) / n * 100`, rounded to 1 decimal.
- **Timed out/given up %** = `(count(timeout) + count(error)) / n * 100`. This groups deadline expiry and queue rejection together as "we gave up."

#### Retries per request
- **Function:** `table_2b()` in `metrics.py:222`
- **Source column:** `retries`
- **Math:** `sum(retries) / n`, rounded to 2 decimals.

#### Peak queue length
- **Function:** `_peak_concurrency(rows)` in `metrics.py:96`
- **Source columns:** `start_time` + `total_ms`
- **Math:** Event sweep. Create events: (+1) at each request start, (-1) at each request end. Sort by timestamp, with ends (-1) before starts (+1) at the same timestamp. Sweep, tracking running sum, return max.
- **Why ends before starts:** A request finishing exactly when another starts should not count as concurrent.
- **Note:** This is a *lower bound* — rejected requests (no interval) are excluded. The scheduler's own peak (in traces) may be higher.

#### Tokens per answer
- **Function:** `table_2b()` in `metrics.py:184-192` and `table_3()` in `metrics.py:277-281`
- **Source columns:** `tokens_in` + `tokens_out`
- **Math:** `(sum(tokens_in) + sum(tokens_out)) / count(ok_rows)`, rounded to 1 decimal.
- **Missing handling:** Only `status=ok` rows included. Failed requests (0 tokens) are excluded, not averaged in as zero.
- **Example (steady):** 6 ok rows, total tokens = 10,947 → 10,947/6 = 1,824.5.

#### Cost per 1,000 answers
- **Function:** `table_2b()` in `metrics.py:195-206`
- **Source columns:** `tokens_in`, `tokens_out` (ok rows only)
- **Math:** `(sum(tokens_in) * 0.15/1e6 + sum(tokens_out) * 0.60/1e6) / count(ok_rows) * 1000`, rounded to 4 decimals.
- **Constants:** `PRICE_INPUT_PER_MILLION = 0.15`, `PRICE_OUTPUT_PER_MILLION = 0.60` (Groq's published paid-plan prices). Free tier costs $0; this is shadow cost only.
- **Example (steady):** (10,947 * 0.15/1e6) / 6 * 1000 = $0.3713.

#### Correct % / Wrong % / Refused correctly % / Refused wrongly %
- **Function:** `table_3()` in `metrics.py:260-290`
- **Source column:** `answer_grade`
- **Math:** `count(grade=X) / n * 100`, rounded to 1 decimal.
- **Note:** `correct` already requires a citation (enforced by grader). `refused_wrongly` counts answerable questions the assistant refused.

#### Tokens per answer (Table 3)
- Same as Table 2b: average of (tokens_in + tokens_out) over ok rows, rounded to 1 decimal.

#### TTFT p50/p95 (Table 3)
- Same percentile function as Table 2a, but over quality rows only.

#### n (questions asked)
- **Function:** `table_3()` in `metrics.py:260`
- **Math:** `len(group)` where group = rows matching (setting, source) with scenario=quality.

#### grade_counts (overall grades)
- **Function:** `grade_counts()` in `metrics.py:307`
- **Source column:** `answer_grade`
- **Math:** Counter of all grade values, including zero-count grades. Returns dict with all 5 keys (correct, wrong, refused_correctly, refused_wrongly, ungraded).

#### Duration (Table 2a, duration_s field)
- Same as `_duration_seconds` above, rounded to 1 decimal.

---

## Part 2 — The 90 tests across 12 files

### test_targeting.py — 12 tests, 89 lines
**File:** `tests/test_targeting.py`
**Tests:** `the_relay/rag/targeting.py`
**What it tests:** Paper detection from question wording, distinctive term extraction, corpus-absence check, stemming.
**Motive:** Pins the deterministic paper targeting that prevents Moshi passages from answering Transformer questions.

| Test | What it verifies |
|---|---|
| `test_moshi_is_detected_as_strong_target` | "audio dataset used to train Moshi" → doc_id=moshi, explicit=True |
| `test_h_net_is_dynamic_chunking_paper` | "H-Net routing module" → dynamic_chunking, explicit=True |
| `test_named_transformer_paper_is_explicit` | "In the Transformer paper" → attention, explicit=True |
| `test_bare_transformer_is_only_a_weak_hint` | "How many attention heads does the Transformer use?" → attention, explicit=False (weak hint only) |
| `test_rq_transformer_belongs_to_moshi_not_attention` | "RQ-Transformer in Moshi" → moshi (not attention) |
| `test_plural_library_wording_disables_narrowing` | "Which of these papers..." → None (plural disables narrowing) |
| `test_cross_paper_comparison_disables_narrowing` | "Compare Moshi and the Transformer" → None |
| `test_no_paper_named_and_no_weak_hint_returns_none` | "capital of France" → None |
| `test_distinctive_terms_drop_framing_words_and_numbers` | Framing words ("paper", "describe") and numbers dropped from distinctive terms |
| `test_absent_terms_detects_topic_missing_from_body` | "ImageNet" absent from dynamic_chunking body → detected |
| `test_absent_terms_is_empty_when_topic_is_present` | Topic present → empty absent list |
| `test_stemming_matches_plural_and_gerund_forms` | "masking" stems to same as "mask" |

### test_evidence.py — 6 tests, 75 lines
**File:** `tests/test_evidence.py`
**Tests:** `the_relay/core/evidence.py` → `assess_evidence()`
**What it tests:** Evidence sufficiency gate (sufficient/partial/insufficient).
**Motive:** Proves refusal happens before spending tokens when evidence is weak.

| Test | What it verifies |
|---|---|
| `test_no_chunks_is_insufficient` | Empty chunk list → insufficient |
| `test_absent_topic_terms_force_refusal` | Corpus-absence terms → insufficient, even with a retrieved passage |
| `test_strong_single_passage_is_sufficient` | High-relevance passage with matching terms → sufficient |
| `test_numeric_question_without_a_number_is_partial` | "BLEU score" question but passage has no number → partial |
| `test_comparison_from_one_document_is_partial` | Cross-paper comparison from one doc → partial |
| `test_weak_evidence_is_insufficient` | Low relevance (0.01) → insufficient |

### test_grader.py — 10 tests, 84 lines
**File:** `tests/test_grader.py`
**Tests:** `the_relay/eval/grader.py` → `explain_grade()`, `grade_answer()`, `has_citation()`, `is_refusal()`
**What it tests:** 4-value grading contract, fact-before-refusal ordering, infrastructure failure grading.
**Motive:** Pins BUG-005 (grader misgraded caveats as refusals).

| Test | What it verifies |
|---|---|
| `test_fully_supported_answer_with_citation_is_correct` | All facts + citation → correct |
| `test_correct_answer_with_caveat_is_still_correct` | Facts + caveat → still correct (BUG-005 fix) |
| `test_refusal_on_answerable_question_is_refused_wrongly` | Pure refusal on answerable → refused_wrongly |
| `test_answer_missing_a_fact_is_wrong` | Missing fact → wrong, missing_groups tracked |
| `test_facts_without_citation_are_wrong` | Facts but no citation → wrong, cited=False |
| `test_unanswerable_question_refused_is_refused_correctly` | Unanswerable + refusal → refused_correctly |
| `test_unanswerable_question_answered_is_wrong` | Unanswerable + answer → wrong |
| `test_infrastructure_failure_grade_depends_on_answerability` | rate_limited → wrong (answerable) / refused_correctly (unanswerable) |
| `test_number_matching_does_not_leak_into_neighbouring_number` | "8" doesn't match inside "28.4" or "18" |
| `test_citation_and_refusal_helpers` | has_citation() and is_refusal() work |

### test_quota.py — 9 tests, 107 lines
**File:** `tests/test_quota.py`
**Tests:** `the_relay/core/quota.py` → `QuotaManager`, `QuotaLimits`
**What it tests:** Token-aware admission, rolling windows, release/cancel, boundary retry, daily limits.
**Motive:** Pins BUG-002, BUG-003, BUG-004, BUG-007.

| Test | What it verifies |
|---|---|
| `test_admits_within_all_limits` | Normal admission succeeds |
| `test_reserve_fraction_reduces_effective_limit` | 50% reserve → effective limit halved; never zero |
| `test_token_limit_rejects_and_reports_wait` | TPM exceeded → rejected with wait_s > 0 |
| `test_release_reconciles_estimate_with_actual_usage` | Release replaces estimate with actual; more fits |
| `test_cancel_frees_a_reservation_that_was_never_sent` | Cancel frees reservation |
| `test_daily_token_limit_is_reported_separately` | TPD exceeded → "daily token" reason (BUG-007) |
| `test_deadline_expiry_raises_instead_of_waiting_forever` | Short deadline → QuotaExceededError |
| `test_zero_wait_boundary_is_retried_not_rejected` | Boundary (wait_s=0) → retried, not rejected (BUG-003) |
| `test_snapshot_reports_remaining_daily_budget` | Snapshot shows correct remaining tokens |

### test_retry.py — 7 tests, 95 lines
**File:** `tests/test_retry.py`
**Tests:** `the_relay/core/retry.py` → `RetryPolicy`
**What it tests:** Retry classification (temporary vs permanent), backoff, deadline awareness.
**Motive:** Proves harmful retries (immediate 429 retry) are avoided.

| Test | What it verifies |
|---|---|
| `test_rate_limit_is_retried_when_budget_allows` | 429 retried when deadline allows |
| `test_retry_after_is_honoured_but_capped` | retry_after_s respected but capped at max_delay |
| `test_permanent_error_is_never_retried` | 401 → never retried |
| `test_daily_quota_is_never_retried` | Daily quota → never retried (waiting can't restore) |
| `test_retry_limit_is_enforced` | Max retries enforced |
| `test_backoff_that_crosses_the_deadline_is_rejected` | Backoff exceeding deadline → rejected |
| `test_missing_token_budget_blocks_retry` | No token budget → retry blocked |

### test_scheduler.py — 5 tests, 104 lines
**File:** `tests/test_scheduler.py`
**Tests:** `the_relay/core/scheduler.py` → `BoundedScheduler`
**What it tests:** Concurrency cap, bounded queue, deadline rejection.
**Motive:** Pins BUG-008 (max_queue_depth=0).

| Test | What it verifies |
|---|---|
| `test_runs_work_and_measures_queue_and_execution` | Basic run works, measures queue/execution time |
| `test_concurrency_cap_is_not_exceeded` | max_concurrency=1 → peak in-flight = 1 even with 4 concurrent tasks |
| `test_full_queue_is_rejected_immediately` | Full queue → QueueFullError immediately (BUG-008) |
| `test_expired_deadline_is_rejected_before_queueing` | Past deadline → DeadlineExceededError before queueing |
| `test_execution_that_exceeds_the_deadline_raises` | Slow execution past deadline → DeadlineExceededError |

### test_results_csv.py — 11 tests, 132 lines
**File:** `tests/test_results_csv.py`
**Tests:** `the_relay/obs/results_csv.py` → `ResultsWriter`, `read_results`, `write_rows_atomic`
**What it tests:** Strict 12-column CSV contract enforcement.
**Motive:** The brief says reviewers recompute tables from this file; malformed rows would make everything unrecomputable.

| Test | What it verifies |
|---|---|
| `test_header_is_exactly_the_required_columns` | Header matches RESULTS_COLUMNS exactly |
| `test_required_schema_has_the_brief_s_exact_names` | Frozen tuple matches brief's 12 named columns |
| `test_append_then_read_round_trip` | Write then read returns same data |
| `test_missing_measurement_is_an_empty_cell_not_zero` | None → empty cell, not "0" |
| `test_duplicate_request_id_is_rejected` | Duplicate request_id raises ValueError |
| `test_unknown_status_is_rejected` | Invalid status (e.g. "success") raises |
| `test_unknown_source_is_rejected` | Invalid source (e.g. "mock") raises |
| `test_unknown_grade_is_rejected` | Invalid grade (e.g. "excellent") raises |
| `test_missing_or_extra_column_is_rejected` | Missing/extra column raises |
| `test_wrong_header_on_existing_file_is_detected` | Wrong header on existing file raises |
| `test_atomic_rewrite_resets_the_file` | Atomic rewrite replaces all rows |

### test_cache.py — 6 tests, 98 lines
**File:** `tests/test_cache.py`
**Tests:** `the_relay/core/cache.py` → `ExactCache`, `CacheEntry`
**What it tests:** Exact cache safety (version-aware key, TTL, eviction, disabled mode).
**Motive:** Proves the cache can never serve a stale answer from a different corpus/prompt/model.

| Test | What it verifies |
|---|---|
| `test_miss_then_hit` | First get misses, second get hits |
| `test_key_changes_with_every_meaningful_setting` | Key changes when preset/model/prompt/corpus/temp/max_tokens/source change |
| `test_key_is_stable_for_the_same_inputs` | Same inputs → same key |
| `test_disabled_cache_always_misses` | Disabled cache never hits |
| `test_expired_entry_is_a_miss` | Expired entry → miss + expired counter |
| `test_oldest_entry_is_evicted_at_capacity` | LRU eviction at capacity |

### test_verify.py — 9 tests, 82 lines
**File:** `tests/test_verify.py`
**Tests:** `the_relay/core/verify.py` → `verify_answer`, `normalise_citation_brackets`, `split_sentences`, `extract_citation_ids`, `build_partial_answer`
**What it tests:** Citation normalisation, sentence splitting, claim verification, partial answer construction.
**Motive:** Pins BUG-006 (full-width brackets) and proves verification catches fabricated numbers.

| Test | What it verifies |
|---|---|
| `test_full_width_citation_brackets_are_normalised` | 【S1】 → [S1] (BUG-006) |
| `test_citation_ids_are_extracted_in_order` | Extracts S1, S3, S2 from "[S1] and B [S3, S2]" |
| `test_abbreviations_do_not_split_sentences` | "e.g." doesn't split a sentence |
| `test_supported_claim_passes` | Claim with matching number + citation → passes |
| `test_invalid_citation_is_rejected` | [S9] with only S1 available → invalid_citations=('S9',) |
| `test_uncited_claim_is_rejected` | Claim without citation → unsupported_claims |
| `test_cited_but_unsupported_number_is_rejected` | [S1] but number doesn't match → 0 supported |
| `test_pure_refusal_passes_without_claims` | Refusal with no claims → passes (claim_count=0) |
| `test_partial_answer_keeps_only_supported_sentences` | build_partial_answer keeps supported, drops unsupported |

### test_standin.py — 5 tests, 99 lines
**File:** `tests/test_standin.py`
**Tests:** `the_relay/providers/standin.py` → `StandInProvider`
**What it tests:** Determinism, citation emission, streaming callback, scheduled failures, refusal on missing evidence.
**Motive:** Proves the stand-in is reproducible and exercises failure paths.

| Test | What it verifies |
|---|---|
| `test_standin_is_deterministic` | Same seed → same answer (two providers) |
| `test_standin_emits_citation_and_uses_sources` | Answer contains [S1] and "8", usage.output_tokens > 0 |
| `test_standin_streaming_invokes_the_callback` | on_token callback receives all pieces; joined = response.text |
| `test_scheduled_rate_limit_is_raised` | rate_limit_on_calls=(1,) → raises on first call |
| `test_missing_evidence_returns_a_refusal_not_a_guess` | No evidence → refusal, no [S#] citation |

### test_pipeline_e2e.py — 4 tests, 131 lines
**File:** `tests/test_pipeline_e2e.py`
**Tests:** `the_relay/core/pipeline.py` → `Relay.ask()` (full pipeline)
**What it tests:** End-to-end integration with real indexes and stand-in provider.
**Motive:** Proves all stages compose correctly.

| Test | What it verifies |
|---|---|
| `test_grounded_answer_is_cited_and_written_to_csv` | Grounded answer has citation, tokens > 0, CSV row written correctly |
| `test_question_about_absent_topic_is_refused_without_a_model_call` | Out-of-scope refusal, model_calls=0, no citation |
| `test_exact_cache_serves_the_second_identical_question` | First miss, second hit, same answer, 2 CSV rows |
| `test_provider_rate_limit_maps_to_rate_limited_status` | Scheduled 429 → status=rate_limited, tokens_in>0 (conservative), tokens_out=0 |

### test_metrics.py — 6 tests, 119 lines
**File:** `tests/test_metrics.py`
**Tests:** `the_relay/eval/metrics.py` → `table_2a`, `table_2b`, `table_3`, `grade_counts`
**What it tests:** Population filtering (quality vs load rows), source separation, refusal inclusion in denominator.
**Motive:** Pins the fix that separated quality rows from load rows in tables.

| Test | What it verifies |
|---|---|
| `test_table_2a_excludes_quality_rows` | Quality rows don't appear in Table 2a |
| `test_table_2b_excludes_quality_rows` | Quality rows don't appear in Table 2b |
| `test_table_3_uses_only_quality_rows_and_separates_source` | Table 3 uses quality rows only, separates by source |
| `test_table_3_percentages_include_refusals_in_the_denominator` | Refusals count in denominator for percentages |
| `test_grade_counts_includes_zeroes` | All 5 grade keys present even with zero counts |
| `test_out_of_scope_refusal_records_the_requested_preset` | Scope refusal records preset, not "none" (BUG-009) |

### test_evidence.py — 6 tests, 75 lines
**File:** `tests/test_evidence.py`
**Tests:** `the_relay/core/evidence.py` → `assess_evidence()`, `refusal_message()`
**What it tests:** Evidence sufficiency gate logic.
**Motive:** Proves refusal happens before spending tokens.

| Test | What it verifies |
|---|---|
| `test_no_chunks_is_insufficient` | Empty chunks → insufficient |
| `test_absent_topic_terms_force_refusal` | Corpus-absence terms → insufficient |
| `test_strong_single_passage_is_sufficient` | High relevance + matching terms → sufficient |
| `test_numeric_question_without_a_number_is_partial` | Numeric question, no number in passage → partial |
| `test_comparison_from_one_document_is_partial` | Cross-paper comparison from one doc → partial |
| `test_weak_evidence_is_insufficient` | Low relevance (0.01) → insufficient |

---

## Summary: 90 tests across 12 files

| File | Tests | Lines | What it targets |
|---|---:|---:|---|
| test_targeting.py | 12 | 89 | Paper detection, distinctive terms, corpus absence, stemming |
| test_results_csv.py | 11 | 132 | Strict 12-column CSV contract |
| test_grader.py | 10 | 84 | 4-value grading, facts-before-refusal, number boundaries |
| test_quota.py | 9 | 107 | Token-aware admission, rolling windows, boundary retry |
| test_verify.py | 9 | 82 | Citation normalisation, claim verification, partial answers |
| test_metrics.py | 6 | 119 | Table population filtering, source separation, preset recording |
| test_evidence.py | 6 | 75 | Evidence sufficiency gate logic |
| test_cache.py | 6 | 98 | Exact cache safety, TTL, eviction, version-aware keys |
| test_scheduler.py | 5 | 104 | Concurrency cap, bounded queue, deadline rejection |
| test_standin.py | 5 | 99 | Determinism, streaming, scheduled failures, refusal |
| test_retry.py | 7 | 95 | Retry classification, backoff, deadline awareness |
| test_pipeline_e2e.py | 4 | 131 | End-to-end integration with real indexes |

**Total: 90 tests, ~1,205 lines of test code, ~2 seconds runtime, zero network/quota.**