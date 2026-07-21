"""Credit budget — spec §18.3.

"Per-sweep ceilings on Apollo credits, PDL credits, Tavily calls, and LLM
spend. On exhaustion the sweep **pauses and alerts** — it does not silently
process a shorter list."

`CreditBudget` is a single, shared, in-process counter meant to be
constructed once per sweep and passed to every stage that spends a metered
resource (Stage 6 Apollo, Stage 8 PDL, Stage 9 Tavily; Stage 10 LLM tokens
join this in Phase 4). This codebase has no sweep orchestrator yet (spec
§16.1's `sweep()` is later-phase work — see `main.py`'s module docstring),
so today a `CreditBudget` is constructed once per CLI invocation (one
company); the object's contract doesn't change once a real multi-company
sweep exists, only how many `process_company` calls share one instance.

"Alerts" (§18.3, §19.6: "Page — sweep is incomplete") means paging an
on-call system in production. No paging integration exists in this codebase
(the same is true of every other "page"/"alert" reference in the existing
§17 failure taxonomy — e.g. `schema_violation`'s "page on-call" is not wired
to a real pager anywhere either). `CreditBudget` logs the exhaustion at
`error` level and reports it via `BudgetStatus`, which is as far as this
implementation goes; wiring a real alert channel is out of scope here, same
as it is for every other unwired "alert" in this project's existing scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from gtm_agent.config import get_settings
from gtm_agent.core.logging import get_logger

logger = get_logger(__name__)


class BudgetMeter(StrEnum):
    APOLLO_CREDITS = "apollo_credits"
    PDL_CREDITS = "pdl_credits"
    TAVILY_CALLS = "tavily_calls"
    LLM_TOKENS = "llm_tokens"  # unused until Phase 4 (Stage 10) — declared now for schema stability


# Conservative defaults — no budget figures are given by the spec (open
# question §23.14: "what is the actual monthly allowance?"). These exist so
# `CreditBudget()` is usable out of the box in the CLI demo harness and in
# tests; a real deployment should set real ceilings from the actual vendor
# contracts once §23.14 has an answer.
_DEFAULT_CEILINGS: dict[BudgetMeter, int] = {
    BudgetMeter.APOLLO_CREDITS: 500,
    BudgetMeter.PDL_CREDITS: 500,
    BudgetMeter.TAVILY_CALLS: 500,
    BudgetMeter.LLM_TOKENS: 1_000_000,
}


@dataclass
class CreditBudget:
    """Tracks consumption against a per-sweep ceiling for each metered
    service. `try_consume` never raises — spec §18.3's "pauses and alerts"
    is a caller-level decision (stop calling this stage for further
    companies), not an exception-driven control-flow surprise buried in a
    shared utility.
    """

    ceilings: dict[BudgetMeter, int] = field(default_factory=lambda: dict(_DEFAULT_CEILINGS))
    _used: dict[BudgetMeter, int] = field(default_factory=dict, init=False, repr=False)
    _exhausted: set[BudgetMeter] = field(default_factory=set, init=False, repr=False)

    @classmethod
    def from_settings(cls) -> CreditBudget:
        """Ceilings from `config.Settings` (`.env`-backed) rather than the
        hardcoded fallback default — how `main.py` constructs one per sweep.
        """
        settings = get_settings()
        return cls(
            ceilings={
                BudgetMeter.APOLLO_CREDITS: settings.apollo_credit_ceiling,
                BudgetMeter.PDL_CREDITS: settings.pdl_credit_ceiling,
                BudgetMeter.TAVILY_CALLS: settings.tavily_call_ceiling,
                BudgetMeter.LLM_TOKENS: _DEFAULT_CEILINGS[BudgetMeter.LLM_TOKENS],
            }
        )

    def used(self, meter: BudgetMeter) -> int:
        return self._used.get(meter, 0)

    def remaining(self, meter: BudgetMeter) -> int:
        return max(0, self.ceilings.get(meter, 0) - self.used(meter))

    def is_exhausted(self, meter: BudgetMeter) -> bool:
        return meter in self._exhausted

    def summarize(self) -> dict[str, dict[str, int]]:
        """Spec §22 Phase 5's "cost tuning" starting point: per-meter
        used/ceiling/remaining, attributable per sweep — spec §18.3: "cost
        is attributable per company and per stage rather than visible only
        as a monthly invoice." Not itself a cost-tuning recommendation
        (that needs real usage history across sweeps, which a single
        `CreditBudget` instance doesn't hold); this is the per-sweep figure
        such an analysis would aggregate across many sweeps.
        """
        return {
            meter.value: {
                "used": self.used(meter),
                "ceiling": self.ceilings.get(meter, 0),
                "remaining": self.remaining(meter),
            }
            for meter in BudgetMeter
        }

    def try_consume(self, meter: BudgetMeter, amount: int = 1) -> bool:
        """Attempt to spend `amount` units of `meter`. Returns `False` (and
        does not spend) if doing so would exceed the ceiling; `True` (and
        records the spend) otherwise. The first call that would exceed the
        ceiling logs the exhaustion once — spec §18.3/§19.6: "Page — sweep is
        incomplete" — never repeatedly for every subsequent rejected call.
        """
        if self.used(meter) + amount > self.ceilings.get(meter, 0):
            if meter not in self._exhausted:
                self._exhausted.add(meter)
                logger.error(
                    "budget_exhausted",
                    meter=meter.value,
                    ceiling=self.ceilings.get(meter, 0),
                    used=self.used(meter),
                    requested=amount,
                )
            return False
        self._used[meter] = self.used(meter) + amount
        return True
