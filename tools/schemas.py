"""OpenAI function-calling schemas for the seven tools in tools/registry.py.

Every argument that can be enumerated is enumerated, drawn from the frozen
mart/config layer. resolve_airports' `query` is the one deliberate
exception: it is the system's only free-text entry point.

Each description states both when to use the tool and when not to.
"""
from __future__ import annotations

from tools import data as d

AIRPORT_CODE_ENUM = sorted(d.metrics_df()["code"].tolist())
REGION_ENUM = d.region_names() + ["national"]
PROFILE_ENUM = sorted(d.scoring_config()["profiles"].keys())
TOP_N_ENUM = [5, 10, 15, 20]
COMPARE_DIMENSION_ENUM = ["composite"] + list(d.RAW_METRIC_COLUMNS)

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "resolve_airports",
            "description": (
                "Resolve free-text place names (a city, metro-area nickname, or region) into IATA "
                "airport codes. Use this FIRST whenever the user names a place rather than a 3-letter "
                "code -- 'Boston', 'LA', 'the Bay Area', 'New England'. Returns ambiguous: true with a "
                "list of interpretations when the text could mean more than one distinct airport (e.g. "
                "'LA' -> LAX/BUR/LGB/ONT/SNA); when that happens, ask the user which one they mean "
                "before calling any other tool with a code -- never guess. Do NOT use this for a region "
                "argument to rank_airports (that tool takes the region name directly as an enum) and do "
                "NOT use it if the user already gave you an exact 3-letter IATA code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free text naming a place: a city, airport name, metro nickname, or region name. E.g. 'Boston', 'LA', 'DC', 'New England'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rank_airports",
            "description": (
                "Rank investable airports (large/medium/small hub only -- non_hub airports are excluded "
                "by the investability floor) by investment-candidate composite score, nationally or "
                "within one region, under one weighting profile. Use for 'top N airports', 'best "
                "candidates in <region>', or 'which airports should we look at' questions. Every result "
                "carries its own hub_size cohort label since percentiles are computed within cohort, not "
                "nationally -- a mixed ranking is not apples-to-apples row to row. Do NOT use this for a "
                "single airport's own detail (use airport_profile or explain_score) or for a side-by-side "
                "of specific named airports (use compare_airports)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "enum": REGION_ENUM,
                        "description": "A named US Census region, or 'national' for no region filter.",
                    },
                    "profile": {
                        "type": "string",
                        "enum": PROFILE_ENUM,
                        "description": (
                            "Weighting profile: 'terminal_expansion' (gate/terminal capital, weighs demand "
                            "and strain most), 'airfield_expansion' (runway/taxiway capital, weighs "
                            "feasibility most), or 'general' (equal pillar weights, no investment-type bias)."
                        ),
                    },
                    "top_n": {
                        "type": "integer",
                        "enum": TOP_N_ENUM,
                        "description": "How many top-ranked airports to return.",
                    },
                },
                "required": ["region", "profile", "top_n"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_airports",
            "description": (
                "Side-by-side comparison of 2-5 named airports on ONE dimension at a time (a single raw "
                "metric, or the overall composite score). Returns the raw value as the primary figure and "
                "the hub_size-cohort percentile as a secondary, clearly-labelled figure. Use for 'how does "
                "X compare to Y' questions about specific airports. Call it again with a different "
                "dimension if the user wants more than one aspect compared. Do NOT use this for a ranked "
                "list of many airports (use rank_airports) or airport codes you haven't resolved yet (use "
                "resolve_airports first)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "codes": {
                        "type": "array",
                        "items": {"type": "string", "enum": AIRPORT_CODE_ENUM},
                        "minItems": 2,
                        "maxItems": 5,
                        "description": "2 to 5 IATA airport codes to compare.",
                    },
                    "dimension": {
                        "type": "string",
                        "enum": COMPARE_DIMENSION_ENUM,
                        "description": (
                            "The single dimension to compare on. 'composite' is the overall investability "
                            "score (needs `profile`); the rest are individual raw metrics."
                        ),
                    },
                    "profile": {
                        "type": "string",
                        "enum": PROFILE_ENUM,
                        "description": "Weighting profile, used only when dimension='composite'. Ignored for raw-metric dimensions. Defaults to 'general' if omitted.",
                    },
                },
                "required": ["codes", "dimension"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "airport_profile",
            "description": (
                "Full descriptive snapshot of ONE airport: raw metrics, hub_size-cohort percentile for "
                "every implemented metric, pillar scores, and composite score under all three weighting "
                "profiles at once. Use for open-ended 'tell me about X' questions. Do NOT use this when the "
                "user wants the weighting arithmetic for one specific profile explained (use explain_score) "
                "or a bottleneck classification (use diagnose_unmet_demand)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": AIRPORT_CODE_ENUM, "description": "IATA airport code."},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flight_mix",
            "description": (
                "Carrier, route and haul-length composition for ONE airport from May 2026 T-100 "
                "Segment: top destinations, top carriers, traffic totals, and haul_mix -- the short/"
                "medium/long-haul split with each band's share of departures, passengers and seats, "
                "already computed. Use for 'what does X's traffic look like', 'who flies out of X', "
                "'where does X fly to', and for any haul-length question ('what percentage of flights "
                "out of X are long haul', 'how much of X's traffic is short haul'). Report haul shares "
                "from haul_mix verbatim; never derive a haul percentage yourself from top_destinations "
                "or from your own knowledge of route distances. This is a descriptive tool, not a "
                "scoring tool -- do NOT use it to answer investability, ranking, or capacity-strain "
                "questions (use rank_airports/compare_airports/airport_profile/diagnose_unmet_demand for "
                "those)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": AIRPORT_CODE_ENUM, "description": "IATA airport code."},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diagnose_unmet_demand",
            "description": (
                "Deterministic bottleneck classification for ONE airport: regulatory_constrained, "
                "capacity_trap, airfield_constrained, terminal_constrained, latent_demand_unconstrained, "
                "strain_without_demand_pressure, or unconstrained -- plus the evidence and a plain-language "
                "explanation for the label. Use when the user asks WHY an airport is (or isn't) a good "
                "renovation candidate, or wants to know what kind of constraint it faces. Do NOT use this "
                "for a plain score (use explain_score) or a plain snapshot (use airport_profile) -- use it "
                "specifically for the causal/diagnostic question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": AIRPORT_CODE_ENUM, "description": "IATA airport code."},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_score",
            "description": (
                "The full weighting arithmetic behind ONE airport's composite score under ONE profile: "
                "every metric's raw value and percentile, each pillar's score, its weight in the profile, "
                "its weight after renormalization (only relevant when a pillar is unavailable for that "
                "airport), and its contribution to the composite. Use when the user asks 'why did X score "
                "Y' or wants the math shown, not just the number. Do NOT use this for a bottleneck "
                "diagnosis (use diagnose_unmet_demand) or a multi-profile overview (use airport_profile)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": AIRPORT_CODE_ENUM, "description": "IATA airport code."},
                    "profile": {
                        "type": "string",
                        "enum": PROFILE_ENUM,
                        "description": "Which weighting profile's arithmetic to show.",
                    },
                },
                "required": ["code", "profile"],
            },
        },
    },
]
