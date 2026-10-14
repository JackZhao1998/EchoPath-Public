# Agent handoff — benchmarking the three ways of resolving a click

This is for whoever picks the work up next, human or agent. It covers how to run
the benchmark, what the numbers currently are, and the traps that cost a full
run each when they were found. Read the traps before writing a new task; every
one of them produced a plausible, publishable, wrong result.

## The question being measured

When replay reaches a click, something has to decide where the pointer goes.
There are three ways, and they cost very different amounts:

| rung | how it decides | cost |
|---|---|---|
| saved patch | template-matches a 52×52 crop kept from the demonstration | free, ~50 ms |
| Decision Model (Jev) | picks among OCR text lines, returns a calibrated confidence | ~$0.00005, ~300 ms |
| vision model | reads a screenshot, returns a click point | ~16,000 tokens, ~5 s |

The three benchmark conditions are:

- **`echopath`** — patch, falling back to the vision model. What ships today.
- **`jev-only`** — Jev alone, no patch and no vision fallback.
- **`echopath+jev`** — patch → Jev → vision. The proposed ladder.

Jev is `typesafe/jev-1.13` on OpenRouter. It cannot see: it chooses among text
options produced by Apple's Vision framework (`VNRecognizeTextRequest`) over a
screenshot. That constraint is the source of most of the interesting results.

## Running it

Install the package and benchmark dependencies first with
`python -m pip install -e ".[macos,evaluation]"`. The runners now live under
`src/echopath/evaluation/` and run as modules. They locate bridges, credentials
and `local_data/` from that source package, independently of the working
directory; set `ECHOPATH_PROJECT_ROOT` to select another package folder.

```bash
.venv/bin/python -m echopath.evaluation.eval_offset --pilot     # 2 variations per task, ~10 min
.venv/bin/python -m echopath.evaluation.eval_offset             # 5 variations per task, ~40 min
.venv/bin/python -m echopath.evaluation.eval_offset --only pubmed-select
.venv/bin/python -m echopath.evaluation.summarise_pareto local_data/offset/results.jsonl
```

`src/echopath/evaluation/eval_offset.py` is the harness to use. The other runners
in that directory (`eval_pareto.py`, `eval_ladder.py` and `eval_steps.py`) are
earlier attempts kept for history; their numbers are not comparable and at
least one of them scores the demonstrated variation (see traps).
`src/echopath/evaluation/summarise_pareto.py` reads families out of the results
file, so it works on any of them.

Before the first run:

- `credentials/api_keys.yaml` needs an `openrouter:` key. Gitignored; never
  print it.
- The terminal running this needs **Accessibility** and **Screen Recording** in
  System Settings → Privacy & Security, or clicks are silently dropped.
- The run drives a real browser and a real mouse for ~40 minutes. It takes over
  the machine.
- Chrome is launched with `--remote-debugging-port`; `src/echopath/cdp.py`
  pins it to the primary display and the harness refuses to start elsewhere.

## Where the numbers stand

60 scored runs, five out-of-sample variations per task, negative controls
passing. Four tasks chosen so the two mechanisms fail in different places:

| what is being clicked | task | patch | Jev |
|---|---|---|---|
| a link whose text changes | open the PubMed result titled X | 4/5 | **5/5** |
| a control beside its label | select that same result | **5/5** | 0/5 |
| a control with no words | mute a Wikimedia Commons video | **5/5** | 0/5 |
| one of many identical | open the diff for one revision | 0/5 | 1/5 |

| condition | finished | vision calls | cost |
|---|---|---|---|
| `echopath` | 14/20 | 5 | $0.2368 |
| `jev-only` | 6/20 | 0 | $0.0022 |
| `echopath+jev` | **15/20** | **0** | **$0.0007** |

The first two rows of the task table run on the same PubMed page, one row
apart, and have opposite winners. That is the strongest part of the result: the
site is not the variable, the target is.

The identical-controls row is the useful negative. The patch matches at
0.98–1.00 and clicks the wrong row *without escalating* — confidently wrong,
which is worse than missing — and Jev can name the right line but OCR returns
`(cur | prev)` as one region, so it cannot aim at `prev` either.

## Traps

Every one of these produced a clean-looking wrong answer.

1. **Do not score the demonstrated variation.** Matching a template against the
   screenshot it was cut from returns ~1.00. It accounted for four of seven crop
   hits in an earlier published table. `main()` evaluates `variations[1:]`.

2. **The vision rung must return coordinates, not a phrase.** An earlier version
   matched the model's words against *the same OCR rows Jev was given*, so two
   of the three conditions shared the bottleneck under test.

3. **Check the vision rung actually ran.** It once called a module that does not
   exist and swallowed the error, so every escalation silently made no click and
   recorded zero cost. Confirm a run with non-zero `tokens` before trusting any
   result where the patch is expected to miss.

4. **Read the right token keys.** The bridge emits `input_tokens` and
   `output_tokens`, not `total_tokens`. Reading the wrong one prices the
   expensive rung at zero.

5. **Scroll the target into view.** OCR only sees the viewport. A target below
   the fold is not in the option list, so Jev is asked to choose something it was
   never shown, and fails for a reason that has nothing to do with the model.
   Tasks carry a `prepare_js` that scrolls first.

6. **Give options their context.** Offering bare OCR regions hands the model
   fifty options that all read `prev`. Each option now carries the line it sits
   on, which is what a person sees.

7. **Jev can only aim at text.** `Row.center` returns the leftmost region's
   centre. A control that is not its own text region is unreachable even when
   the right row is chosen. Distinguish "chose wrong" from "chose right, clicked
   wrong" before calling it a model failure.

8. **Score page state, never pixels.** A pixel oracle is template matching,
   which is the mechanism under test, and it hands the patch arm the answer.
   Tasks read state over DevTools Protocol.

9. **Keep the negative control.** Every task clicks a point proven by
   `elementFromPoint` to be a different element; if the oracle still passes, the
   task is reported unscorable rather than scored. This caught three of the bugs
   above. It is currently **skipped for `commons-mute`** — the control bar is
   too dense for the offsets tried — so that column's oracle is unverified.

10. **Reset between runs.** PubMed remembers selected results across
    navigations, so an absolute-state oracle passes without any click. Oracles
    compare before and after.

## Known gaps

- **The matcher measured is not the shipping one.** The harness uses a
  single-scale greyscale template match at threshold 0.74.
  `src/echopath/visual_grounding.py` sweeps scales 0.5–2.0, offers edge and
  colour modes, rejects ambiguous matches, and uses 0.78. Routing the crop rung
  through it is the highest-value next change, and it may move the
  identical-controls result, since the ambiguity check exists to refuse exactly
  that match.
- **The ladder is not wired into the product.** `memory_replay.py` still goes
  patch → vision. Everything here is a standalone harness.
- **The accessibility tree was never properly tested.** `ax_extractor.py` says
  Chrome needs `--force-renderer-accessibility`; that flag appears nowhere else
  in the repo and no harness imports the module. If AX returns `AXButton
  "Zoom in"`, the "wordless targets are unreachable" premise weakens and the
  architecture claim becomes "use AX where the app publishes it".
- **Five variations per cell, one run each.** Enough to show the failure modes
  are real and disjoint; not enough for a confidence interval.

## Adding a task

`Task` in `src/echopath/evaluation/eval_offset.py` takes: `url_for`, a `question` or `question_from`
(built from the pre-click state), `demo_point_js` (where a person would point —
read from the page, never searched for), optional `prepare_js` / `prepare_point_js`,
`reset_js`, `state_js`, and `succeeded(before, after)`. Use `option_mode="regions"`
when the target is one word on a line. Variations may carry an index as
`"name|3"`, which `render()` substitutes into the JavaScript as `__IDX__`.

Keep the demonstration a demonstration: one look, one click, at a point the page
reports. An earlier design clicked up to twelve candidates until one worked,
which replaces learning-from-one-demonstration with search.
