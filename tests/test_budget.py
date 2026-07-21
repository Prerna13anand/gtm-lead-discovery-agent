"""Credit budget tests — spec §18.3."""

from gtm_agent.leads.budget import BudgetMeter, CreditBudget


def test_fresh_budget_has_full_ceiling_remaining():
    budget = CreditBudget(ceilings={BudgetMeter.APOLLO_CREDITS: 10})
    assert budget.remaining(BudgetMeter.APOLLO_CREDITS) == 10
    assert budget.used(BudgetMeter.APOLLO_CREDITS) == 0
    assert budget.is_exhausted(BudgetMeter.APOLLO_CREDITS) is False


def test_try_consume_within_ceiling_succeeds_and_tracks_usage():
    budget = CreditBudget(ceilings={BudgetMeter.APOLLO_CREDITS: 10})
    assert budget.try_consume(BudgetMeter.APOLLO_CREDITS, 4) is True
    assert budget.used(BudgetMeter.APOLLO_CREDITS) == 4
    assert budget.remaining(BudgetMeter.APOLLO_CREDITS) == 6


def test_try_consume_exceeding_ceiling_fails_and_does_not_spend():
    budget = CreditBudget(ceilings={BudgetMeter.APOLLO_CREDITS: 10})
    assert budget.try_consume(BudgetMeter.APOLLO_CREDITS, 11) is False
    assert budget.used(BudgetMeter.APOLLO_CREDITS) == 0
    assert budget.is_exhausted(BudgetMeter.APOLLO_CREDITS) is True


def test_try_consume_exactly_at_ceiling_succeeds():
    budget = CreditBudget(ceilings={BudgetMeter.APOLLO_CREDITS: 10})
    assert budget.try_consume(BudgetMeter.APOLLO_CREDITS, 10) is True
    assert budget.remaining(BudgetMeter.APOLLO_CREDITS) == 0
    # One more unit now exceeds it, even by 1.
    assert budget.try_consume(BudgetMeter.APOLLO_CREDITS, 1) is False


def test_meters_are_independent():
    budget = CreditBudget(ceilings={BudgetMeter.APOLLO_CREDITS: 1, BudgetMeter.PDL_CREDITS: 1})
    assert budget.try_consume(BudgetMeter.APOLLO_CREDITS, 1) is True
    # Exhausting Apollo must not affect PDL's independent ceiling.
    assert budget.try_consume(BudgetMeter.APOLLO_CREDITS, 1) is False
    assert budget.try_consume(BudgetMeter.PDL_CREDITS, 1) is True


def test_never_silently_truncates_never_partially_spends_on_a_rejected_call():
    budget = CreditBudget(ceilings={BudgetMeter.APOLLO_CREDITS: 5})
    assert budget.try_consume(BudgetMeter.APOLLO_CREDITS, 3) is True
    # This would exceed the ceiling by 3; rejected wholesale, not partially spent.
    assert budget.try_consume(BudgetMeter.APOLLO_CREDITS, 5) is False
    assert budget.used(BudgetMeter.APOLLO_CREDITS) == 3


def test_summarize_reports_used_ceiling_and_remaining_per_meter():
    budget = CreditBudget(ceilings={BudgetMeter.APOLLO_CREDITS: 10, BudgetMeter.PDL_CREDITS: 5})
    budget.try_consume(BudgetMeter.APOLLO_CREDITS, 4)
    summary = budget.summarize()
    assert summary[BudgetMeter.APOLLO_CREDITS.value] == {"used": 4, "ceiling": 10, "remaining": 6}
    assert summary[BudgetMeter.PDL_CREDITS.value] == {"used": 0, "ceiling": 5, "remaining": 5}


def test_from_settings_reads_configured_ceilings(monkeypatch):
    from gtm_agent.config.settings import Settings

    monkeypatch.setattr(
        "gtm_agent.leads.budget.get_settings",
        lambda: Settings(apollo_credit_ceiling=7, pdl_credit_ceiling=8, tavily_call_ceiling=9),
    )
    budget = CreditBudget.from_settings()
    assert budget.ceilings[BudgetMeter.APOLLO_CREDITS] == 7
    assert budget.ceilings[BudgetMeter.PDL_CREDITS] == 8
    assert budget.ceilings[BudgetMeter.TAVILY_CALLS] == 9
