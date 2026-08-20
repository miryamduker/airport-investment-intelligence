"""The seven agent tools, in one dispatch table.

agent/loop.py looks tools up here by name; tools/schemas.py declares the
matching OpenAI function schemas. The two must stay in step -- a name in one
and not the other is a bug.
"""
from __future__ import annotations

from tools.diagnostics import airport_profile, diagnose_unmet_demand, explain_score
from tools.lookup import resolve_airports
from tools.ranking import compare_airports, rank_airports
from tools.traffic import flight_mix

TOOL_DISPATCH = {
    "resolve_airports": resolve_airports,
    "rank_airports": rank_airports,
    "compare_airports": compare_airports,
    "airport_profile": airport_profile,
    "flight_mix": flight_mix,
    "diagnose_unmet_demand": diagnose_unmet_demand,
    "explain_score": explain_score,
}
