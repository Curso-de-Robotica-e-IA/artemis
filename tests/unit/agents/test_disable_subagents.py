from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

from artemis.agents.flash.runner import FlashRunner
from artemis.config.agent import AgentGlobalConfig, ExplorerConfig, is_explorer_enabled, is_tool_denylisted
from artemis.context import ArtemisContext, ExecutionSetup


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    mock_llm_cfg = Mock()
    mock_llm_cfg.model = "gemini-2.5-flash"
    mock_llm_cfg.temperature = 0.1
    ctx.llm_config.get_agent.return_value = mock_llm_cfg

    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    ctx.data_engine = None
    ctx.adb_client = None
    ctx.driver = Mock()
    return ctx


def test_is_explorer_enabled_default():
    ctx = Mock(spec=ArtemisContext)
    ctx.agent_config = AgentGlobalConfig()
    ctx.execution_setup = ExecutionSetup()
    assert is_explorer_enabled(ctx, "operator") is True
    assert is_explorer_enabled(ctx, "flash") is True


def test_is_explorer_enabled_when_disabled():
    ctx = Mock(spec=ArtemisContext)
    cfg = AgentGlobalConfig.model_validate({
        "explorer": {"enabled": False}
    })
    ctx.agent_config = cfg
    ctx.execution_setup = ExecutionSetup(explorer=cfg.explorer)
    assert is_explorer_enabled(ctx, "operator") is False
    assert is_explorer_enabled(ctx, "flash") is False


def test_is_explorer_enabled_when_denylisted():
    ctx = Mock(spec=ArtemisContext)
    cfg = AgentGlobalConfig.model_validate({
        "denylisted_tools": {
            "operator": ["ask_explorer"],
            "flash": ["ask_explorer"]
        }
    })
    ctx.agent_config = cfg
    ctx.execution_setup = ExecutionSetup()
    assert is_explorer_enabled(ctx, "operator") is False
    assert is_explorer_enabled(ctx, "flash") is False


def test_flash_runner_omits_ask_explorer_when_disabled(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        mock_context.agent_config = AgentGlobalConfig.model_validate({
            "explorer": {"enabled": False}
        })
        mock_context.execution_setup = ExecutionSetup(explorer=mock_context.agent_config.explorer)

        runner = FlashRunner(mock_context, goal="test goal")
        tools = runner._get_tools()
        names = [t.name for t in tools]

        assert "ask_explorer" not in names
        assert "click" in names
        assert "swipe" in names

        prompt = runner._render_system_prompt(tools)
        assert "use the `ask_explorer` tool" not in prompt


def test_flash_runner_omits_denylisted_tools(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        mock_context.agent_config = AgentGlobalConfig.model_validate({
            "denylisted_tools": {
                "flash": ["ask_explorer", "swipe"]
            }
        })
        mock_context.execution_setup = ExecutionSetup()

        runner = FlashRunner(mock_context, goal="test goal")
        tools = runner._get_tools()
        names = [t.name for t in tools]

        assert "ask_explorer" not in names
        assert "swipe" not in names
        assert "click" in names


def test_resolve_operator_prompt_tools_without_subagents():
    from artemis.agents.operator.prompts import resolve_operator_prompt_tools

    ctx = Mock(spec=ArtemisContext)
    ctx.data_engine = None
    ctx.actuator = None
    ctx.execution_setup = ExecutionSetup(
        video_recording_tools_enabled=False,
        explorer=ExplorerConfig(enabled=False),
    )
    ctx.agent_config = AgentGlobalConfig.model_validate({
        "explorer": {"enabled": False},
        "video_analyzer": {"enabled": False},
        "denylisted_tools": {"operator": ["ask_diagnoser"]}
    })

    available = resolve_operator_prompt_tools(ctx)
    assert "ask_explorer" not in available
    assert "video_analyzer" not in available
    assert "ask_diagnoser" not in available
    assert "click" in available
    assert "swipe" in available
