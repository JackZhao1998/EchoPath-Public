"""The workflows and input variations the eval runs over.

Kept apart from the harness so the case set can grow without touching the
measurement code. Each workflow's first variation is spent recording its
reference, so a workflow needs N+1 variations to yield N scored cases.

Two shapes of page are represented deliberately, because they are the two the
earlier scenarios separated on:

* ``table_row`` -- a real results table (EDGAR). Structure alone should find the
  target, no model needed.
* ``first_product`` -- a page of nested groups with no table (Amazon). Structure
  cannot rank the candidates, so this is where a decision model has to earn its
  place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class Case:
    workflow: str
    variation: str
    url: str
    question: str          # asked of whichever model has to decide
    expect: str            # regex the chosen text must match
    reject: str = ""       # regex it must not match
    skip_above: float = 0.55   # ignore rows in this top fraction (page furniture)

EDGAR = "https://www.sec.gov/edgar/search/#/q={q}"
AMAZON = "https://www.amazon.com/s?k={q}"

# Companies with steady EDGAR presence, so a missing-results run means a real
# failure rather than an empty query.
EDGAR_QUERIES = [
    "tesla", "nvidia", "apple", "microsoft", "amazon", "alphabet",
    "meta platforms", "netflix", "intel", "oracle", "salesforce", "adobe",
]

# Everyday consumables: stable listings, and the sponsored rows that make
# "the first product" genuinely ambiguous.
AMAZON_QUERIES = [
    "paper towels", "coffee filters", "aa batteries", "dish soap",
    "trash bags", "printer paper", "hand soap", "sponges",
    "aluminum foil", "laundry detergent", "toilet paper", "paper plates",
]

# Filing forms EDGAR actually returns, so a correct row matches one of them.
FILING_PATTERN = r"(10-K|10-Q|8-K|S-1|DEF 14A|424B|6-K|20-F|13F|SC 13|424|N-|497|144|NPORT|FWP|425)"


def build_cases(*, pilot: bool = False) -> List[Case]:
    edgar_queries = EDGAR_QUERIES[:5] if pilot else EDGAR_QUERIES
    amazon_queries = AMAZON_QUERIES[:5] if pilot else AMAZON_QUERIES

    cases: List[Case] = []

    for query in edgar_queries:
        cases.append(Case(
            workflow="edgar",
            variation=query,
            url=EDGAR.format(q=query.replace(" ", "+")),
            question=(
                "Which row is the first filing result in the search results table? "
                "It names a filing form, a date, and the filing entity."
            ),
            expect=FILING_PATTERN,
            skip_above=0.66,
        ))

    for query in amazon_queries:
        cases.append(Case(
            workflow="amazon",
            variation=query,
            url=AMAZON.format(q=query.replace(" ", "+")),
            question=(
                "Which row is the first genuine product listing in the search results? "
                "Ignore navigation, account links, filters, category links, and "
                "anything marked Sponsored."
            ),
            # A real listing names the thing searched for. Keyed off the query so
            # a plausible-looking but unrelated link does not count as a hit.
            expect=r".{20,}",
            reject=r"(sponsored|sign in|your orders|shop all|see more|filters?$|cart)",
        ))

    return cases
