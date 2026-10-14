# United Airlines flight search demo

On September 5, 2026 (UTC), Codex GPT-6 Astra completed this task using native
Windows computer use and EchoPath MCP:

> Open Chrome from the Windows desktop, go to United Airlines, and search
> one-way IAD to SFO on November 10, 2026 for 2 adults and 1 child aged 2–17 in
> Business or First. Stop at loaded flight listings.

The successful runs reached actual IAD–SFO flight cards with United First
fares. The traveler panel showed 2 adults, 1 child aged 2–17, and zero infants.
No flight was booked. These are observations from an existing browser profile,
not a claim that every browser state or future page layout is supported.

## Successful runs and learning

| Phase | Seconds | Model calls | Input tokens | Cached input (subset) | Output tokens | Total tokens | API token estimate (USD) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Native computer use, full Codex turn | 507.131 | 33 | 6,086,025 | 5,872,384 | 7,212 | 6,093,237 | $8.369394 |
| EchoPath corrected first pass, execution | 712.300 | 58 | 1,238,037 | 796,416 | 8,962 | 1,246,999 | $5.660726 |
| First-pass automatic consolidation | 118.313 | 1 | 20,954 | 0 | 2,981 | 23,935 | $0.358590 |
| EchoPath successful replay, execution | 64.946 | 1 | 17,892 | 0 | 289 | 18,181 | $0.193370 |
| Replay automatic consolidation | 205.907 | 1 | 20,671 | 14,976 | 4,851 | 25,522 | $0.314476 |

The native browser-to-listings interval was **380.111 seconds**. Its token
count covers the full turn from 09:35:37.719Z to 09:44:04.850Z, including setup
and reporting. Tokens were recovered from Codex's task log: the difference
between cumulative totals matched the sum of all 33 request records. The
6,093,237 total includes 5,872,384 cached input tokens and is not a count of
newly generated text.

EchoPath execution starts at the MCP call and ends when the recorded GUI run
finishes. It includes routing and memory lookup; automatic consolidation is
measured separately. Tokens cover EchoPath backend calls only, excluding the
surrounding Codex conversation, diagnostics, and manual memory edits. The
backend usage records all identify `gpt-6-astra`.

The full first-pass MCP call, including learning, took **830.629 seconds** and
used **1,270,934 tokens**, estimated at **$6.019316**. The full successful replay
call took **270.861 seconds** and used **43,703 tokens**, estimated at
**$0.507846**. Small differences between call duration and phase sums are gaps
between execution and consolidation.

For execution alone, replay used 98.54% fewer tokens and was 10.97 times faster
than the corrected recording. Both ran 22 actions. The replay used 11
direct memory actions and 11 visually re-aimed memory actions, with no
grounding or planning fallbacks. Native use and EchoPath have different
accounting scopes, and each successful run is a single observation.

## API token cost calculation

Rates were checked against the official
[GPT-6 Astra model pricing](https://developers.openai.com/api/docs/models/gpt-6-astra)
on September 5, 2026. Standard rates per million tokens are $10 for uncached
input, $1 for cached input, and $50 for output. Cache writes cost $12.50 per
million. Requests above 272,000 input tokens use higher rates; the largest
native request had 219,516 input tokens and the largest EchoPath request in
this repair session had 23,943, so that adjustment does not apply here.

With zero cache-write tokens, the calculation is:

```text
uncached_input = input_tokens - cached_input_tokens
estimated_USD = (uncached_input * 10 + cached_input_tokens * 1
                 + output_tokens * 50) / 1,000,000
```

For example, the native turn costs
`(213,641 × 10 + 5,872,384 × 1 + 7,212 × 50) / 1,000,000 = $8.369394`.
Replay execution costs
`(17,892 × 10 + 0 × 1 + 289 × 50) / 1,000,000 = $0.193370`.

The native log reports zero cache-write tokens. EchoPath's token logger does
not retain cache-write counts, so its estimates assume zero; if some uncached
input was written to cache, add $2.50 per million such tokens. Cached input
and reasoning output are subsets of input and output respectively and are
not added again. Prices use Standard service, without Fast-mode premiums or
Batch/Flex discounts, and exclude non-token tool charges. These are
API-equivalent estimates, not an invoice or Codex subscription charges.

## Earlier repair work

The README shows successful execution rows. This repair session also included
the following work, which is excluded from those rows:

| Phase | Seconds | Input tokens | Cached input (subset) | Output tokens | Total tokens | API token estimate (USD) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Earlier recording, execution | 623.962 | 1,188,098 | 627,712 | 8,752 | 1,196,850 | $6.669172 |
| Earlier recording, automatic consolidation | 169.039 | 20,683 | 0 | 3,970 | 24,653 | $0.405330 |
| Manual consolidation call | 136.869 | 20,772 | 0 | 3,383 | 24,155 | $0.376870 |
| Replay interrupted by page-load failure | 54.375 | 38,019 | 14,720 | 457 | 38,476 | $0.270560 |

Across the entire recorded EchoPath repair session, including the successful
recording and replay and all consolidation calls, the totals are
**2,598,771 tokens**, **2,085.849 seconds of MCP calls**, and **$14.249094** in
estimated API token cost. This excludes the native comparison, host Codex
orchestration, manual repair time, and attempts before this repair session.

## Conditions of the tested replay

The tested memory starts by opening Chrome from a Windows desktop shortcut
and navigates to United. Its source recording was repaired to include browser
and page-load waits, address-bar focus, destination clearing, and traveler
Reset before recording the successful replay.

- The existing browser profile loads a form with **One-way, Washington IAD,
  and Business or First already selected**. The memory does not set these
  three controls itself.
- Opening the departure calendar exposes **November 10, 2026** at the recorded
  selection position, without additional month navigation.
- Traveler Reset restores 1 adult, zero children, and zero infants; the
  replay then adds one adult and one child.
- The desktop shortcut position, display scaling, browser zoom, and United
  layout match the recording. No blocking browser setup or site overlay
  appears.
- Chrome is ready within 3 seconds, the United form within 10 seconds,
  destination suggestions within 2 seconds, and flight listings within 10
  seconds after submission.
- The task uses a 200-step limit and captures the screen after each action.

Passenger counts and loaded results were independently checked from the
recorded screenshots. Post-replay learning produced a separate candidate;
the successful replay tested the preceding 22-action memory. Raw task logs,
screenshots, and this machine-specific memory remain local and are not part
of the public demo.
