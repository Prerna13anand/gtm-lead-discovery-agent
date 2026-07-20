"""core.run_ledger tests — spec §15.1, §2.3. Uses tmp_path so nothing is
ever written to the real `.data/` directory during tests.
"""

import json
from pathlib import Path

from gtm_agent.core.run_ledger import ScrapeRunLedger, archive_raw_payloads
from gtm_agent.models.scrape_run import ScrapeRunStatus


def test_begin_run_creates_an_open_run_with_no_status(tmp_path: Path):
    ledger = ScrapeRunLedger(tmp_path / "scrape_runs.jsonl")
    run = ledger.begin_run("acme")

    assert run.company_id == "acme"
    assert run.status is None  # spec §17: not terminated yet
    assert run.finished_at is None
    assert run.id  # a real id was assigned


def test_close_run_sets_terminal_fields_and_persists(tmp_path: Path):
    ledger_path = tmp_path / "scrape_runs.jsonl"
    ledger = ScrapeRunLedger(ledger_path)

    run = ledger.begin_run("acme")
    run.source_id = "https://acme.com/careers"
    closed = ledger.close_run(
        run,
        status=ScrapeRunStatus.SUCCESS,
        jobs_found=5,
        http_requests_made=2,
        bytes_fetched=1234,
        adapter_used="greenhouse",
    )

    assert closed.status == ScrapeRunStatus.SUCCESS
    assert closed.finished_at is not None
    assert closed.jobs_found == 5
    assert closed.http_requests_made == 2
    assert closed.bytes_fetched == 1234
    assert closed.adapter_used == "greenhouse"
    assert ledger_path.exists()


def test_a_stage_1_failure_still_produces_a_recorded_run(tmp_path: Path):
    """spec §2.3: a company we couldn't scrape must stay visible, not vanish."""
    ledger = ScrapeRunLedger(tmp_path / "scrape_runs.jsonl")
    run = ledger.begin_run("acme")
    ledger.close_run(run, status=ScrapeRunStatus.NO_CAREERS_PAGE, failure_detail="ladder exhausted")

    runs = ledger.list_runs()
    assert len(runs) == 1
    assert runs[0].status == ScrapeRunStatus.NO_CAREERS_PAGE
    assert runs[0].failure_detail == "ladder exhausted"


def test_list_runs_round_trips_multiple_entries(tmp_path: Path):
    ledger = ScrapeRunLedger(tmp_path / "scrape_runs.jsonl")

    run1 = ledger.begin_run("acme")
    ledger.close_run(run1, status=ScrapeRunStatus.SUCCESS, jobs_found=3)

    run2 = ledger.begin_run("beta")
    ledger.close_run(run2, status=ScrapeRunStatus.DOMAIN_UNREACHABLE)

    all_runs = ledger.list_runs()
    assert len(all_runs) == 2
    assert {r.company_id for r in all_runs} == {"acme", "beta"}

    acme_runs = ledger.list_runs(company_id="acme")
    assert len(acme_runs) == 1
    assert acme_runs[0].jobs_found == 3


def test_list_runs_on_a_fresh_ledger_is_empty(tmp_path: Path):
    ledger = ScrapeRunLedger(tmp_path / "does_not_exist_yet.jsonl")
    assert ledger.list_runs() == []


def test_each_run_gets_its_own_line_and_id(tmp_path: Path):
    ledger_path = tmp_path / "scrape_runs.jsonl"
    ledger = ScrapeRunLedger(ledger_path)

    run1 = ledger.begin_run("acme")
    ledger.close_run(run1, status=ScrapeRunStatus.SUCCESS)
    run2 = ledger.begin_run("acme")
    ledger.close_run(run2, status=ScrapeRunStatus.SUCCESS)

    lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    ids = {json.loads(line)["id"] for line in lines}
    assert len(ids) == 2  # distinct ids, not overwritten


def test_archive_raw_payloads_writes_keyed_by_company_and_run(tmp_path: Path):
    ref = archive_raw_payloads(
        "acme",
        "run-123",
        [{"title": "Engineer"}, {"title": "Designer"}],
        base_dir=tmp_path,
    )

    assert ref == "acme/run-123"
    archived_path = tmp_path / "acme" / "run-123.json"
    assert archived_path.exists()
    contents = json.loads(archived_path.read_text(encoding="utf-8"))
    assert contents == [{"title": "Engineer"}, {"title": "Designer"}]
