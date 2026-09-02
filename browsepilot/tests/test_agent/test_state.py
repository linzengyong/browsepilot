from browsepilot.agent.state import AgentState, ExecutionStep


def test_state_defaults():
    s = AgentState()
    assert s.task == ""
    assert s.plan == []
    assert s.total_steps == 0
    assert s.stop_reason == ""
    assert s.degradation_log == []


def test_state_degradation_log_roundtrip():
    entry = {"strategy": "weather", "error": "city_not_found", "message": "无此城市"}
    s = AgentState(degradation_log=[entry])
    assert s.degradation_log == [entry]


def test_state_with_task_and_fields():
    s = AgentState(task="查天气", intent="browser_task", tier="tier1")
    assert s.task == "查天气"
    assert s.tier == "tier1"


def test_execution_step_defaults():
    step = ExecutionStep(index=0, tool="navigate")
    assert step.status == "running"
    assert step.duration_ms is None
    assert step.arguments == {}


def test_state_roundtrip_dump_load():
    s = AgentState(
        task="t",
        execution_log=[ExecutionStep(index=0, tool="x", status="success")],
    )
    d = s.model_dump()
    assert d["execution_log"][0]["tool"] == "x"
    assert AgentState(**d) == s
