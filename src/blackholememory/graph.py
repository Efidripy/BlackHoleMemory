from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class AppState(TypedDict, total=False):
    action: str
    status: str


def _bootstrap_node(state: AppState) -> AppState:
    next_state = dict(state)
    next_state["status"] = "graph-ready"
    return next_state


def build_graph():
    graph = StateGraph(AppState)
    graph.add_node("bootstrap", _bootstrap_node)
    graph.add_edge(START, "bootstrap")
    graph.add_edge("bootstrap", END)
    return graph.compile()
