# greenhouse_langgraph_demo.py
from __future__ import annotations

from typing import TypedDict, List, Dict, Any, Optional
from copy import deepcopy

from langgraph.graph import StateGraph, START, END


# ---------------------------
# 2) State (single source of truth)
# ---------------------------
class GHState(TypedDict, total=False):
    # Live/derived telemetry
    telemetry: Dict[str, float]        # e.g., {"vpd_now": 1.35, "soil_theta": 0.28, "dli_today": 12.7}
    # Proposals from agents
    proposal: Dict[str, Any]           # {"climate": {...}, "irrigation": {...}}
    # Policies / guardrails
    policies: Dict[str, Any]           # {"fertigation_lockout_min": 20, "protected_limits": ["heater_max"]}
    # Metrics & verification
    metrics: Dict[str, float]          # {"risk_index": 34}
    # Control flags and artifacts
    reviewer_notes: Optional[str]
    approved: Optional[bool]
    logs: List[str]


# ---------------------------
# 3) Mock tools (stand-ins)
# ---------------------------
def weather_forecast_tool() -> Dict[str, float]:
    return {"cloud_cover": 0.7, "temp_out": 26.0}

def compute_kpis(state: GHState) -> Dict[str, float]:
    vpd = state["telemetry"].get("vpd_now", 1.0)
    soil = state["telemetry"].get("soil_theta", 0.32)
    dli  = state["telemetry"].get("dli_today", 18.0)
    risk = (1.0 if vpd < 0.5 else 0.0) + (1.0 if soil < 0.28 else 0.0) + (1.0 if dli < 18 else 0.0)
    return {"risk_index": risk * 20}   # 0–60 toy scale


# ---------------------------
# 4) Nodes (read/write state)
# ---------------------------
def append_log(state: GHState, msg: str) -> GHState:
    s = deepcopy(state)
    s.setdefault("logs", []).append(msg)
    return s

def planner_node(state: GHState) -> GHState:
    s = append_log(state, "Planner: evaluating telemetry & deciding routes")
    vpd  = s["telemetry"]["vpd_now"]
    soil = s["telemetry"]["soil_theta"]
    needs = []
    if soil < 0.30:
        needs.append("irrigation")
    if vpd < 0.8 or vpd > 1.2:
        needs.append("climate")
    s["proposal"] = {"needs": needs}
    return append_log(s, f"Planner: needs={needs or ['none']}")

def climate_node(state: GHState) -> GHState:
    s = append_log(state, "Climate: proposing hourly setpoints to hit VPD 0.9–1.1")
    forecast = weather_forecast_tool()
    s["proposal"]["climate"] = {
        "setpoints_hourly": [{"hour": 9, "temp": 23.0, "rh": 68, "co2": 850}],
        "notes": f"cloud_cover={forecast['cloud_cover']:.2f}"
    }
    return s

def irrigation_node(state: GHState) -> GHState:
    s = append_log(state, "Irrigation: scheduling safe micro-pulses to raise θv → 0.32")
    s["proposal"]["irrigation"] = {
        "pulses": [
            {"time": "08:15", "vol_l": 25, "ec": 2.2},
            {"time": "09:15", "vol_l": 25, "ec": 2.2}
        ],
        "lockout_min": state["policies"].get("fertigation_lockout_min", 20)
    }
    return s

def verifier_node(state: GHState) -> GHState:
    s = append_log(state, "Verifier: computing KPIs and checking conflicts")
    s["metrics"] = compute_kpis(s)
    risk = s["metrics"]["risk_index"]
    # Toy policy: touching climate proposal or high risk requires HITL
    needs_hitl = ("climate" in s["proposal"]) or (risk >= 30)
    s = append_log(s, f"Verifier: risk_index={risk:.1f}, HITL={'yes' if needs_hitl else 'no'}")
    s["proposal"]["needs_hitl"] = needs_hitl
    return s

def hitl_gate_node(state: GHState) -> GHState:
    s = append_log(state, "HITL: awaiting approval for proposed plan")
    # Real app would pause and wait for a human decision.
    # This demo pauses the run if no decision has been provided yet.
    return s

def decide_node(state: GHState) -> GHState:
    # Pure router/junction; no state changes
    return state

def commit_node(state: GHState) -> GHState:
    s = append_log(state, "Commit: applying plan to SCADA proxy (demo: no-op)")
    return s

def revise_node(state: GHState) -> GHState:
    s = append_log(state, "Revise: reviewer requested changes → shrinking adjustments")
    if "climate" in s["proposal"]:
        s["proposal"]["climate"]["setpoints_hourly"][0]["rh"] = 70
    if "irrigation" in s["proposal"]:
        s["proposal"]["irrigation"]["pulses"] = s["proposal"]["irrigation"]["pulses"][:1]
    # Clear decision so the next pass pauses at HITL again
    s["approved"] = None
    return s


# ---------------------------
# 5) Graph wiring
# ---------------------------
graph = StateGraph(GHState)

graph.add_node("Planner", planner_node)
graph.add_node("Climate", climate_node)
graph.add_node("Irrigation", irrigation_node)
graph.add_node("Verifier", verifier_node)
graph.add_node("HITL", hitl_gate_node)
graph.add_node("Decide", decide_node)
graph.add_node("Commit", commit_node)
graph.add_node("Revise", revise_node)

# Entry → Planner
graph.add_edge(START, "Planner")

# Conditional fan-out from Planner
def need_climate(state: GHState) -> bool:
    return "climate" in state.get("proposal", {}).get("needs", [])

def need_irrigation(state: GHState) -> bool:
    return "irrigation" in state.get("proposal", {}).get("needs", [])

graph.add_conditional_edges("Planner", need_climate, {True: "Climate", False: "Irrigation"})
graph.add_conditional_edges("Climate", need_irrigation, {True: "Irrigation", False: "Verifier"})
graph.add_conditional_edges("Irrigation", lambda s: True, {True: "Verifier"})  # always verify

# Verifier → either HITL or Commit
def requires_hitl(state: GHState) -> bool:
    return bool(state.get("proposal", {}).get("needs_hitl", False))

graph.add_conditional_edges("Verifier", requires_hitl, {True: "HITL", False: "Commit"})

# HITL behavior:
# If there's no decision yet, stop (END). If a decision exists, go to Decide.
def has_decision(s: GHState) -> bool:
    return s.get("approved") is not None

graph.add_conditional_edges("HITL", has_decision, {True: "Decide", False: END})

# Decide → Commit (approved) or Revise (rejected)
graph.add_conditional_edges("Decide", lambda s: s.get("approved", False), {True: "Commit", False: "Revise"})

# After Revise, verify again
graph.add_edge("Revise", "Verifier")

# End of flow
graph.add_edge("Commit", END)

app = graph.compile()


# ---------------------------
# 6) Two demo runs
# ---------------------------
def run_stream(state: GHState) -> GHState:
    """
    LangGraph's .stream() yields a sequence of event dicts like {node_name: state}.
    We'll fold them to get the latest state each time for printing/logging.
    """
    last = state
    for event in app.stream(last):
        for _, st in event.items():
            last = st
    return last

if __name__ == "__main__":
    # DEMO A: Pause at HITL → reject → pause → approve → commit
    initial_state: GHState = {
        "telemetry": {"vpd_now": 1.35, "soil_theta": 0.28, "dli_today": 12.7},
        "policies": {"fertigation_lockout_min": 20, "protected_limits": ["heater_max"]},
        "logs": []
    }

    # First pass → no decision set → run halts at HITL (END)
    s = initial_state.copy()
    s = run_stream(s)

    # Reviewer rejects → resume
    s["approved"] = False
    s = run_stream(s)

    # Reviewer approves → resume and commit
    s["approved"] = True
    s = run_stream(s)

    print("\n=== DEMO A LOG (pause → reject → pause → approve → commit) ===")
    print("\n".join(s["logs"]))

    # DEMO B: Easy conditions → Verifier skips HITL → Commit directly
    s2: GHState = {
        "telemetry": {"vpd_now": 0.95, "soil_theta": 0.31, "dli_today": 19.5},
        "policies": {"fertigation_lockout_min": 20, "protected_limits": ["heater_max"]},
        "logs": []
    }
    s2 = run_stream(s2)

    print("\n=== DEMO B LOG (auto-commit) ===")
    print("\n".join(s2["logs"]))

    # Write a Mermaid diagram of the graph
    def write_mermaid(filename: str = "graph.mmd") -> None:
        """
        Writes a Mermaid diagram of the known nodes/edges to graph.mmd.
        Open it in VS Code (with a Mermaid plugin) or render with mermaid-cli.
        """
        mermaid = """flowchart TD
          START([START]) --> Planner
          Planner -->|needs climate| Climate
          Planner -->|else| Irrigation
          Climate -->|needs irrigation| Irrigation
          Climate -->|else| Verifier
          Irrigation --> Verifier
          Verifier -->|HITL = yes| HITL
          Verifier -->|HITL = no| Commit
          HITL -->|decision present| Decide
          HITL -->|no decision| END
          Decide -->|approved| Commit
          Decide -->|rejected| Revise
          Revise --> Verifier
          Commit --> END([END])
        """
        with open(filename, "w") as f:
            f.write(mermaid)
        print(f"Wrote Mermaid diagram to {filename}")

    write_mermaid()
