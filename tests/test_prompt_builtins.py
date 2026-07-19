from mewcode_agent.prompting.builtins import (
    BUILTIN_MODULES,
    FINAL_ROUND_TEXT,
    PLANNING_FULL_TEXT,
    PLANNING_REMINDER_TEXT,
)


def test_builtin_modules_have_exact_ids_priorities_and_protection() -> None:
    assert [
        (item.module_id, item.priority, item.protected)
        for item in BUILTIN_MODULES
    ] == [
        ("core.identity", 100, True),
        ("core.runtime_protocol", 150, True),
        ("behavior.default", 200, False),
        ("tools.default_guidance", 300, False),
        ("core.tool_execution", 400, True),
        ("coding.default_standards", 500, False),
        ("core.authorization", 600, True),
        ("core.safety", 700, True),
        ("output.default_style", 800, False),
    ]


def test_builtin_runtime_text_uses_confirmed_round_rules() -> None:
    assert PLANNING_FULL_TEXT.startswith("当前请求处于规划模式。")
    assert PLANNING_REMINDER_TEXT.startswith("提醒：当前仍处于规划模式。")
    assert FINAL_ROUND_TEXT.startswith("这是当前请求允许的最后一轮。")
    assert "不得请求任何工具" in FINAL_ROUND_TEXT
