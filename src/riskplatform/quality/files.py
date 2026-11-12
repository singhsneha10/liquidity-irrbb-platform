"""File-level checks for a quality run, on the rows that passed row-level validation.

The DuckDB checks in riskplatform.validation.check_raw fall into three groups here:

- STOP: the dataset as a whole is unusable, so nothing may be published (the run stops).
- WARNING: a consistency problem across days. It goes in the breaks report; the run continues.
- Generator only (not run here): checks of how the synthetic bank is built, which real data
  need not meet. 'Every position every day' would also fail as soon as one row is
  quarantined; missing rows are caught by reconciling to the source's control totals instead.
"""

from dataclasses import dataclass

import pandas as pd

from riskplatform.config import GeneratorConfig
from riskplatform.quality.rows import FileLevelError
from riskplatform.validation.check_raw import CheckResult, run_checks_on_tables

STOP_CHECKS = (
    "files_hold_their_own_date",  # a file holding the wrong date would misplace a whole day
    "day_count_matches_config",  # a missing day changes every total and trend
    "positions_and_rates_share_dates",  # a day without curves cannot be priced
)
WARNING_CHECKS = (
    "static_attributes_unchanged",  # e.g. a position switching currency or category
    "day_counts_fall_by_calendar_days",  # maturities or resets that jump unexpectedly
    "rate_changes_only_at_reset",  # a rate that moved with no reset due
)


@dataclass(frozen=True)
class FileCheckReport:
    stops: list[CheckResult]
    warnings: list[CheckResult]


class FileChecksFailed(FileLevelError):
    """At least one STOP check failed. Carries every result, so warnings are still reported."""

    def __init__(self, report: FileCheckReport):
        self.report = report
        failed = [f"{r.name} ({r.failures} offending rows)" for r in report.stops if not r.passed]
        super().__init__("positions and rates", "all files", failed)


def check_files(
    positions: pd.DataFrame, rates: pd.DataFrame, cfg: GeneratorConfig
) -> FileCheckReport:
    """Run the STOP and WARNING checks; raise FileChecksFailed if any STOP check fails.

    positions needs a 'filename' column naming each row's source file.
    """
    results = run_checks_on_tables(positions, rates, cfg, only=STOP_CHECKS + WARNING_CHECKS)
    by_name = {result.name: result for result in results}
    report = FileCheckReport(
        stops=[by_name[name] for name in STOP_CHECKS],
        warnings=[by_name[name] for name in WARNING_CHECKS],
    )
    if not all(result.passed for result in report.stops):
        raise FileChecksFailed(report)
    return report
