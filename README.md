# property_qa

A data ingestion and QA pipeline. It takes messy property-record files from
several sources (a county CSV export, a broker's JSON API feed, a web scraper's
JSON Lines output) and produces:

| Output | What's in it |
|---|---|
| `output/clean.jsonl` | Records that passed every check. Each one carries its source file/line and a log of every fix applied. |
| `output/quarantine.jsonl` | Records that didn't, with the **original raw input** and **every** reason. |
| `output/report.json` | Counts per file, top rejection reasons, fixes applied, and whether the batch was published or held. |

Example outputs from the sample data are in [`sample_output/`](sample_output/).
Built with AI-assisted development (Claude Code).

**The guarantee:** every record in `clean.jsonl` matches the schema, and no
value in it was made up. Every input row ends up in exactly one place
(clean, quarantine, or counted as an exact duplicate), so nothing disappears
silently.

**What it does *not* guarantee:** that the data is *true*. A price of $5 that
should have been $500,000 passes if it's well-formed. Validation proves
conformance, not correctness.

## Run it

```bash
uv sync                                                   # install dependencies (once)
uv run python -m property_qa data/raw --out output        # main demo
uv run python -m property_qa data/format_change_demo --out output_fc   # a source changes format
uv run pytest                                             # 120 tests
```

Main demo result: `PUBLISHED: 55 rows read, 31 clean, 23 quarantined, 1 exact duplicates dropped (42% rejected)`.
The sample data is deliberately much dirtier than a real feed. Every
quarantined row is a planted defect.

Format-change demo: the county "upgraded" its export to day-first dates
overnight. Result: `HELD: 92% of rows were rejected`, exit code 1, and no
`clean.jsonl` is written.

## How it works

```
 files ──► 1. PARSE ──► 2. NORMALIZE ──► 3. VALIDATE ──► file-level checks ──► gate ──► outputs
           parse.py     normalize.py     schema.py       pipeline.py           pipeline.py
```

1. **Parse** (`parse.py`): real parsers (`csv`, `json`) split files into
   rows. Regex never splits files, because quoted commas and nested objects break it.
   A bad *row* costs only that row. A bad *file* (not UTF-8, invalid JSON)
   holds the whole batch.
2. **Normalize** (`normalize.py`): regex at the *field* level. It maps column
   names (`salePrice`, `Sale Price` → `purchase_price`), reads `$1,250,000`,
   `2.1M`, `March 3, 2024`, `California`, and cleans whitespace. **It changes
   notation, never meaning.** Every change is logged as a Fix.
3. **Validate** (`schema.py`): a strict Pydantic model holds the business
   rules: price > 0, date not in the future, valid state, land parcels have
   no bedrooms, and so on. Strict mode means Pydantic won't quietly convert `"3"` to `3`.
   All conversion happens in step 2, where it's logged.
4. **File-level checks** (`pipeline.py`): catch problems no single row can show.
   Conflicting duplicate IDs, and dates whose day/month order the rest of the
   file contradicts.
5. **Gate**: hold the batch if a file was unreadable, there were no rows, or
   the rejection rate exceeds `--max-reject-rate` (default 50%).

## Design decisions

**Why doesn't it auto-fill `CRITICAL_TIMESTAMP_PARSE_ERROR` with the current time?**
Because that invents data. The record would look perfect, and every report
would say the sale happened on the day we ran the script. A visibly broken
record can be fixed. An invisibly wrong one corrupts everything downstream.
Error codes go to quarantine with the raw value attached. This applies even
in optional fields: if the upstream system crashed on this row, its other
fields are suspect too.

**What fixes *are* allowed?** Only ones where the meaning is certain:
`$1,250,000` → `1250000.00`, `California` → `CA`, `03/15/2024` → `2024-03-15`.
Refused: `approx $500k` (an estimate), `3/4/24` (1924 or 2024?), `13/05/2024`
(not a valid US date, so we won't guess it's European), `1.200.000,00`
(European notation).

**The ZIP code example.** Excel turns `02134` into `2134`. Restoring the zero
is safe *only* in states whose ZIPs start with 0 (MA, CT, NJ, …). In
California, `9040` means a digit was lost, and padding it would produce a New
Jersey ZIP. Same input pattern, different answer depending on context.

**Day/month order.** `05/02/2024` is read US-style (May 2), but the
pipeline flags that reading as an assumption. If the same file also contains
`13/05/2024`, which can only be day-first, every flagged date in that file
is withdrawn to quarantine. Without this check, a source that switches to
DD/MM silently swaps day and month for the 1st–12th of every month.

**Conflicting duplicates go to quarantine, *both* of them.** If `CA-100201`
appears twice with different prices, keeping "the first one" is an arbitrary
guess. Exact copies are harmless, so one is kept and the drop is counted.

**Why `Decimal`, not `float`, for money?** In floating point,
`0.1 + 0.2 == 0.30000000000000004`. Prices are written out as exact strings
(`"1250000.00"`).

**Why JSON Lines instead of one JSON array?** One record per line means a
file can be read in a stream, appended to, and split, and a single corrupt
line doesn't make the whole file unreadable. Most data tools (BigQuery,
Snowflake, Spark, `jq`) read it directly.

**Why hold the whole batch at a high rejection rate?** A spike in rejections
usually means the *source* changed (new export format, renamed column), not
that a lot of individual rows went bad. That needs a human, not a partial load.

**Why atomic writes?** Outputs are written to a temp name and then renamed.
The rename is all-or-nothing, so a crash can never leave a half-written file
for the next system to load. A held run also deletes the previous
`clean.jsonl`, so nobody loads stale data thinking it's fresh.

**Why not just use Great Expectations / Pandera / Flatfile?** In a real job
I'd look at them first. Building this shows what they do underneath, and
it covers things a generic validator doesn't know about: domain-aware repairs (ZIP
zeros by state) and cross-row inference (date order across a file).

## Known limitations / next steps

- **Per-source config.** The real fix for date order and encodings is for
  each source to *declare* its format (`county: DD/MM/YYYY, cp1252`) rather
  than the pipeline inferring it. The inference here is a safety net.
  A day-first file whose dates *all* fall on the 1st–12th has no evidence, so it
  would still be misread.
- **Memory.** Accepted records are held in memory until the duplicate check
  finishes. That's fine for files up to millions of rows. Beyond that, deduplicate in the
  database with a unique constraint.
- **Duplicates across runs** aren't detected, only within a batch.
- **Soft warnings.** Things like a $1 sale (common for family transfers) are
  valid but worth flagging rather than rejecting.
- **The reject-rate limit** should be set per source from its history, not
  one global 50%.
