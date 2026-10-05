# Search tuning — embedding model and retrieval cuts

Scripts for choosing the embedding model and the retrieval coefficients an admin sets
under **Admin → Settings → Search → Retrieval**:

| setting | what it is |
|---------|------------|
| `RETRIEVAL_MIN_SCORE` | cosine floor — a hit below it is dropped |
| `RETRIEVAL_SCORE_DROP_OFF` | relevance cliff — a hit scoring below this share of the hit before it ends the list |
| `RETRIEVAL_QUERY_INSTRUCTION` | instruction prefixed to the query (asymmetric embedding models); documents are embedded plain |
| `EMBEDDING_MODEL` | Admin → Settings → Models & APIs |

Both cuts are points on ONE model's score distribution, not properties of your corpus.
Change the model and the old cuts are a different operating point: `qwen3/600m`'s floor of
0.35 under `giga/480m` dropped 10 of 20 correct answers and read as "the new model is
worse". Re-derive the cuts every time the model changes.

Everything here runs against a **test installation**, never one people work in:
`EMBEDDING_MODEL` is process-global and `doc_chunks.embedding` is a single column, so while
a non-default model is active every other project's search on that database is wrong.

## Files

| file | runs | does |
|------|------|------|
| `run.sh` | host | copies the probe and a query set into the backend container, runs it, saves `snapshots/<set>-<label>.json` |
| `probe.py` | backend container | runs each query through the real retrieval, records the rank of the expected document, corpus coverage and the measurement config. Read-only |
| `compare.py` | host, stdlib | `--show` one snapshot, `--diff` two, `--scores` read the cuts off one |
| `arm_embed.py` | backend container | re-embeds a whole project with the model currently configured |
| `xling_seed.py` + `xling_data.json` | backend container | creates (or resets) the synthetic RU/EN bench project |
| `queries/xling-bench.json` | — | 48 queries over the 24 seeded documents |

## A query set

```json
{
  "name": "my-set",
  "project_id": "<project uuid>",
  "queries": [
    {"id": "exact-1", "class": "exact", "query": "…", "expect": "<part of the answer's title>", "why": "…"}
  ]
}
```

`expect` is matched case-insensitively as a substring of the hit's document title. Use
`"class": "memory"` for a question answered by a project-memory fact. Write each query
BEFORE you look at any result and only ever add to a set — a set edited after seeing a
score measures the editor. Your own corpus is the bench that matters; the bundled one
answers only the cross-language question.

## Run one arm

```bash
# 1. In .env: the model under test, both cuts zeroed. Then recreate backend + worker.
#    EMBEDDING_MODEL=<model>  RETRIEVAL_MIN_SCORE=0.0  RETRIEVAL_SCORE_DROP_OFF=0.0
docker compose up -d --force-recreate backend worker

# 2. Seed the synthetic bench (skip for your own project).
B=$(docker compose ps -q backend)
docker cp scripts/search-tuning/xling_seed.py   $B:/tmp/xling_seed.py
docker cp scripts/search-tuning/xling_data.json $B:/tmp/xling_data.json
docker exec $B python /tmp/xling_seed.py

# 3. Re-embed the bench project with this arm's model (6 = concurrency).
docker cp scripts/search-tuning/arm_embed.py $B:/tmp/arm_embed.py
docker exec $B python /tmp/arm_embed.py e11c0000-0000-4000-8000-000000000001 6

# 4. Probe. Wide top_k and budget so a low-ranked answer is still visible.
scripts/search-tuning/run.sh xling-bench <arm-label> --top-k 25 --budget 40000
```

Recreating the container wipes its `/tmp` — copy the scripts again after every model switch.

## Compare models, then set the cuts

```bash
python3 scripts/search-tuning/compare.py --diff scripts/search-tuning/snapshots/xling-bench-A.json scripts/search-tuning/snapshots/xling-bench-B.json
python3 scripts/search-tuning/compare.py --scores scripts/search-tuning/snapshots/xling-bench-B.json
```

`--diff` reports rank movement per query, not score movement — scores are not comparable
across models, ranks are. It refuses to vouch for a pair whose corpus or measurement
config differs. `--scores` prints, for the winning arm, where each correct answer sat
(`score`) and the steepest step between consecutive hits of its kind from the top down
to it (`worst-step` — drop-off cuts at the first cliff, per kind), then the minimum and
quantiles: set `RETRIEVAL_MIN_SCORE` at or below the correct-answer minimum, and
`RETRIEVAL_SCORE_DROP_OFF` at or below the minimum ratio. Then restore the cuts in Admin
and probe once more at the defaults (`run.sh <set> final`) to see what a user gets.

## Four things that silently ruin a measurement

1. **Absolute score cuts left on.** At non-zero cuts a cross-model run measures the
   threshold, not the model. Zero both for EVERY arm, the control included; the snapshot's
   `measurement` line shows what it ran under.
2. **Ranking across kinds.** Retrieval puts memory facts ahead of document chunks, so with
   the cut zeroed `top_k` facts sit in front of every document. The probe ranks a query
   among hits of the kind it targets (`rank_in_kind`); the same arm read MRR 0.23 across
   kinds and 0.86 within.
3. **An incomplete corpus.** Never read a snapshot whose `coverage` is below 1.0 indexed —
   audio references carry their transcript and must be embedded too (`arm_embed.py` has no
   media-type filter for that reason).
4. **An arm that did not switch.** The chunk hash mixes in the model name, so a real switch
   re-embeds everything. `arm_embed.py` ends by printing the stored vectors' `dim` and
   `stored_model`; a `stored_model` that is not the arm's model, or a re-embed that finishes
   suspiciously fast, means the vectors are still the previous model's.

Never name a throwaway script after an importable module: the container's `/tmp` is
`sys.path[0]` for these scripts, and a `/tmp/h2.py` once shadowed the HTTP/2 library.

## Reference result

One corpus, coverage 1.0, cuts zeroed, `--top-k 25 --budget 40000`:

| bench | qwen3/600m | giga/480m |
|-------|-----------|-----------|
| 14 Russian technical queries — MRR / hit@1 | 0.857 / 10 of 14 | 1.0 / 14 of 14 |
| 20 hard queries (exact, morphology, digits, synonym, cross-language, section, memory) — MRR / hit@1 / hit@3 | 0.667 / 11 / 15 | 0.777 / 13 / 17 |
| xling-bench MRR; same-language vs cross-language hit@1 | 0.824; 21/24 vs 14/24 (−29pp) | 0.946; 23/24 vs 21/24 (−8pp) |
| 3000 Russian chars | 1111 tokens, 96 ms | 694 tokens, 26 ms |

giga/480m became the default with `RETRIEVAL_MIN_SCORE` 0.35 → 0.18. Both models miss
queries written in digits against transcripts where speech-to-text spelled the numbers
out — a chunking/query-rewrite problem, not a model choice.
