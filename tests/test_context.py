from c_link.context.schema import ActiveContext, ContextItem, ItemType
from c_link.context.graph import ContextGraph
from c_link.context.compiler import compile_context


def test_decision_supersession():
    ctx = ActiveContext(session_id="s1")
    graph = ContextGraph(ctx)
    graph.add(ContextItem(
        id="d1", type=ItemType.DECISION, content="Use port 9931"
    ))
    graph.add(ContextItem(
        id="d2", type=ItemType.DECISION, content="Use port 9940",
        supersedes="d1"
    ))
    assert graph.active(ItemType.DECISION)[0].content == "Use port 9940"


def test_compiler_contains_current_request():
    ctx = ActiveContext(
        session_id="s1",
        current_objective="Build the context engine",
    )
    packet = compile_context(ctx, "What is the current objective?")
    assert "Build the context engine" in packet
    assert "What is the current objective?" in packet
