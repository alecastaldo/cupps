"""Self-service CUPPS Application conformance testing.

Produces a Compliance Testing Record (Figure 12.1) that an application
supplier can generate for themselves, before asking a platform supplier or
the CTE for formal Application Compliance Testing.

It does not certify anything: section 17.1.2 reserves that to a compliant
platform supplier or the IATA approved Compliance Testing Entity.  What it
does is make the formal test a review of evidence, and catch interface faults
before they burn an integration attempt -- two failures across multiple
suppliers force full re-compliance (section 17.1.2).
"""

from .checks import Check, Finding, catalogue, run_all
from .harness import ConformanceHarness, RunResult, ScenarioStep
from .recorder import Recorder
from .report import to_dict, to_html, to_json, to_text

__all__ = [
    "Check",
    "ConformanceHarness",
    "Finding",
    "Recorder",
    "RunResult",
    "ScenarioStep",
    "catalogue",
    "run_all",
    "to_dict",
    "to_html",
    "to_json",
    "to_text",
]
