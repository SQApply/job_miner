from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .state import JobApplicationState


GraphNode = Callable[[JobApplicationState], JobApplicationState | dict[str, Any]]


def _missing_langgraph_error() -> RuntimeError:
    return RuntimeError(
        "LangGraph is not installed. Install requirements.txt or run: pip install langgraph langgraph-checkpoint-postgres"
    )


def build_job_application_graph(*, nodes: dict[str, GraphNode]):
    try:
        from langgraph.graph import END, StateGraph
    except Exception as exc:  # pragma: no cover - depends on local environment
        raise _missing_langgraph_error() from exc

    graph = StateGraph(JobApplicationState)
    graph.add_node("load_context", nodes["load_context"])
    graph.add_node("preflight_validate", nodes["preflight_validate"])
    graph.add_node("select_strategy", nodes["select_strategy"])
    graph.add_node("prepare_application", nodes["prepare_application"])
    graph.add_node("submit_application", nodes["submit_application"])
    graph.add_node("persist_result", nodes["persist_result"])

    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "preflight_validate")
    graph.add_edge("preflight_validate", "select_strategy")
    graph.add_edge("select_strategy", "prepare_application")
    graph.add_edge("prepare_application", "submit_application")
    graph.add_edge("submit_application", "persist_result")
    graph.add_edge("persist_result", END)
    return graph.compile()


def invoke_job_application_graph(*, initial_state: JobApplicationState, nodes: dict[str, GraphNode]) -> JobApplicationState:
    graph = build_job_application_graph(nodes=nodes)
    config = {"configurable": {"thread_id": initial_state.get("langgraph_thread_id") or initial_state.get("job_run_id")}}
    return graph.invoke(initial_state, config=config)
