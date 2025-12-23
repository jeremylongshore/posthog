from freezegun import freeze_time
from posthog.test.base import APIBaseTest, ClickhouseTestMixin, _create_event, _create_person, flush_persons_and_events
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils.timezone import now

from dateutil.relativedelta import relativedelta
from langchain_core.runnables import RunnableConfig

from products.error_tracking.backend.models import ErrorTrackingIssue, ErrorTrackingIssueFingerprintV2
from products.error_tracking.backend.tools.explain_issue import ExplainErrorTrackingIssueTool
from products.error_tracking.backend.tools.issue_explainer import ErrorTrackingExplainIssueOutput, IssueExplainer

from ee.hogai.context.context import AssistantContextManager
from ee.hogai.utils.types import AssistantState
from ee.hogai.utils.types.base import NodePath


@freeze_time("2025-01-15T12:00:00Z")
class TestExplainErrorTrackingIssueTool(ClickhouseTestMixin, APIBaseTest):
    CLASS_DATA_LEVEL_SETUP = False

    distinct_id_one = "user_1"
    issue_id_one = "01936e7f-d7ff-7314-b2d4-7627981e34f0"

    def setUp(self):
        super().setUp()
        self.tool_call_id = "test_tool_call_id"

        _create_person(
            team=self.team,
            distinct_ids=[self.distinct_id_one],
            is_identified=True,
        )

        self.create_events_and_issue(
            issue_id=self.issue_id_one,
            issue_name="TypeError: Cannot read property 'map' of undefined",
            fingerprint="issue_one_fingerprint",
            distinct_ids=[self.distinct_id_one],
            timestamp=now() - relativedelta(hours=3),
            exception_list=[
                {
                    "type": "TypeError",
                    "value": "Cannot read property 'map' of undefined",
                    "stacktrace": {
                        "frames": [
                            {
                                "resolved_name": "processData",
                                "source": "src/utils/data.js",
                                "line": 42,
                                "column": 15,
                                "in_app": True,
                                "context_line": "return data.map(item => item.value);",
                            },
                            {
                                "resolved_name": "handleClick",
                                "source": "src/components/Button.js",
                                "line": 28,
                                "column": 8,
                                "in_app": True,
                            },
                            {
                                "resolved_name": "onClick",
                                "source": "node_modules/react-dom/cjs/react-dom.development.js",
                                "line": 1234,
                                "in_app": False,
                            },
                        ]
                    },
                }
            ],
        )

        flush_persons_and_events()

    def create_issue(self, issue_id, fingerprint, name=None, status=ErrorTrackingIssue.Status.ACTIVE):
        issue = ErrorTrackingIssue.objects.create(id=issue_id, team=self.team, status=status, name=name)
        ErrorTrackingIssueFingerprintV2.objects.create(team=self.team, issue=issue, fingerprint=fingerprint)
        return issue

    def create_events_and_issue(
        self,
        issue_id,
        fingerprint,
        distinct_ids,
        timestamp=None,
        issue_name=None,
        status=ErrorTrackingIssue.Status.ACTIVE,
        exception_list=None,
    ):
        if timestamp:
            with freeze_time(timestamp):
                self.create_issue(issue_id, fingerprint, name=issue_name, status=status)
        else:
            self.create_issue(issue_id, fingerprint, name=issue_name, status=status)

        event_properties = {"$exception_issue_id": issue_id, "$exception_fingerprint": fingerprint}
        if exception_list:
            event_properties["$exception_list"] = exception_list

        for distinct_id in distinct_ids:
            _create_event(
                distinct_id=distinct_id,
                event="$exception",
                team=self.team,
                properties=event_properties,
                timestamp=timestamp,
            )

    async def _create_tool(self, state: AssistantState | None = None):
        if state is None:
            state = AssistantState(messages=[])

        config: RunnableConfig = RunnableConfig()
        context_manager = AssistantContextManager(team=self.team, user=self.user, config=config)

        tool = await ExplainErrorTrackingIssueTool.create_tool_class(
            team=self.team,
            user=self.user,
            state=state,
            config=config,
            context_manager=context_manager,
            node_path=(NodePath(name="test_node", tool_call_id=self.tool_call_id, message_id="test"),),
        )
        return tool

    def _create_explainer(self):
        return IssueExplainer(team=self.team, user=self.user)

    async def test_returns_error_for_nonexistent_issue(self):
        tool = await self._create_tool()
        nonexistent_id = "00000000-0000-0000-0000-000000000000"

        result_text, artifact = await tool._arun_impl(issue_id=nonexistent_id)

        self.assertIn("not found", result_text)
        self.assertIsNone(artifact)

    async def test_formats_stacktrace_correctly(self):
        explainer = self._create_explainer()

        event = {
            "properties": {
                "$exception_list": [
                    {
                        "type": "TypeError",
                        "value": "Cannot read property 'map' of undefined",
                        "stacktrace": {
                            "frames": [
                                {
                                    "resolved_name": "processData",
                                    "source": "src/utils/data.js",
                                    "line": 42,
                                    "column": 15,
                                    "in_app": True,
                                    "context_line": "return data.map(item => item.value);",
                                },
                                {
                                    "resolved_name": "onClick",
                                    "source": "node_modules/react-dom/cjs/react-dom.development.js",
                                    "line": 1234,
                                    "in_app": False,
                                },
                            ]
                        },
                    }
                ]
            }
        }

        stacktrace = explainer.format_stacktrace(event)

        self.assertIn("Exception 1: TypeError", stacktrace)
        self.assertIn("Cannot read property 'map' of undefined", stacktrace)
        self.assertIn("[IN-APP]", stacktrace)
        self.assertIn("processData", stacktrace)
        self.assertIn("src/utils/data.js:42:15", stacktrace)
        self.assertIn("return data.map(item => item.value);", stacktrace)

    async def test_formats_stacktrace_without_context_line(self):
        explainer = self._create_explainer()

        event = {
            "properties": {
                "$exception_list": [
                    {
                        "type": "ReferenceError",
                        "value": "x is not defined",
                        "stacktrace": {
                            "frames": [
                                {
                                    "resolved_name": "myFunc",
                                    "source": "app.js",
                                    "line": 10,
                                    "in_app": True,
                                }
                            ]
                        },
                    }
                ]
            }
        }

        stacktrace = explainer.format_stacktrace(event)

        self.assertIn("ReferenceError", stacktrace)
        self.assertIn("myFunc", stacktrace)
        self.assertIn("app.js:10", stacktrace)

    async def test_returns_none_for_empty_exception_list(self):
        explainer = self._create_explainer()

        event: dict = {"properties": {"$exception_list": []}}
        stacktrace = explainer.format_stacktrace(event)

        self.assertIsNone(stacktrace)

    async def test_returns_none_for_missing_properties(self):
        explainer = self._create_explainer()

        stacktrace = explainer.format_stacktrace({})
        self.assertIsNone(stacktrace)

        stacktrace = explainer.format_stacktrace(None)
        self.assertIsNone(stacktrace)

    @patch("products.error_tracking.backend.tools.issue_explainer.MaxChatAnthropic")
    async def test_analyze_issue_calls_llm(self, mock_chat_model):
        explainer = self._create_explainer()

        mock_output = ErrorTrackingExplainIssueOutput(
            generic_description="This is a TypeError that occurs when trying to call .map() on undefined.",
            specific_problem="The data variable is undefined when processData is called.",
            possible_resolutions=[
                "Add a null check before calling .map()",
                "Ensure data is initialized properly",
                "Use optional chaining: data?.map()",
            ],
        )

        mock_instance = MagicMock()
        mock_instance.with_structured_output.return_value = mock_instance
        mock_instance.ainvoke = AsyncMock(return_value=mock_output)
        mock_chat_model.return_value = mock_instance

        result = await explainer.analyze_issue("Test stacktrace")

        self.assertEqual(result.generic_description, mock_output.generic_description)
        self.assertEqual(result.specific_problem, mock_output.specific_problem)
        self.assertEqual(result.possible_resolutions, mock_output.possible_resolutions)

    async def test_format_explanation(self):
        explainer = self._create_explainer()

        analysis = ErrorTrackingExplainIssueOutput(
            generic_description="This is a TypeError.",
            specific_problem="Data is undefined.",
            possible_resolutions=["Add null check", "Initialize data"],
        )

        result = explainer.format_explanation(analysis, "TypeError: Cannot read property 'map'")

        self.assertIn("### Issue: TypeError: Cannot read property 'map'", result)
        self.assertIn("This is a TypeError.", result)
        self.assertIn("#### What's happening?", result)
        self.assertIn("Data is undefined.", result)
        self.assertIn("#### How to fix it:", result)
        self.assertIn("1. Add null check", result)
        self.assertIn("2. Initialize data", result)

    @patch("products.error_tracking.backend.tools.issue_explainer.MaxChatAnthropic")
    @patch("products.error_tracking.backend.tools.explain_issue.database_sync_to_async")
    async def test_full_flow_with_mocked_llm(self, mock_db_sync, mock_chat_model):
        tool = await self._create_tool()

        mock_output = ErrorTrackingExplainIssueOutput(
            generic_description="This is a TypeError that occurs when trying to call .map() on undefined.",
            specific_problem="The data variable is undefined when processData is called.",
            possible_resolutions=["Add a null check", "Initialize data properly"],
        )

        mock_instance = MagicMock()
        mock_instance.with_structured_output.return_value = mock_instance
        mock_instance.ainvoke = AsyncMock(return_value=mock_output)
        mock_chat_model.return_value = mock_instance

        # Mock database calls
        mock_issue = MagicMock()
        mock_issue.name = "TypeError: Cannot read property 'map' of undefined"

        mock_event = {
            "properties": {
                "$exception_list": [
                    {
                        "type": "TypeError",
                        "value": "Cannot read property 'map' of undefined",
                        "stacktrace": {
                            "frames": [
                                {
                                    "resolved_name": "processData",
                                    "source": "src/utils/data.js",
                                    "line": 42,
                                    "in_app": True,
                                }
                            ]
                        },
                    }
                ]
            }
        }

        # Mock database_sync_to_async to return callables that return our mocked data
        def mock_db_wrapper(fn, thread_sensitive=False):
            async def wrapper(*args, **kwargs):
                if fn.__name__ == "get_issue":
                    return mock_issue
                elif fn.__name__ == "get_first_event":
                    return mock_event
                return fn(*args, **kwargs)

            return wrapper

        mock_db_sync.side_effect = mock_db_wrapper

        result_text, artifact = await tool._arun_impl(issue_id=self.issue_id_one)

        self.assertIn("TypeError: Cannot read property 'map' of undefined", result_text)
        self.assertIn("This is a TypeError", result_text)
        self.assertIn("How to fix it:", result_text)
        self.assertIsNone(artifact)


class TestIssueExplainerFormatting(APIBaseTest):
    def _create_explainer(self):
        return IssueExplainer(team=self.team, user=self.user)

    async def test_format_stacktrace_multiple_exceptions(self):
        explainer = self._create_explainer()

        event = {
            "properties": {
                "$exception_list": [
                    {
                        "type": "TypeError",
                        "value": "First error",
                        "stacktrace": {
                            "frames": [{"resolved_name": "func1", "source": "file1.js", "line": 1, "in_app": True}]
                        },
                    },
                    {
                        "type": "ReferenceError",
                        "value": "Second error",
                        "stacktrace": {
                            "frames": [{"resolved_name": "func2", "source": "file2.js", "line": 2, "in_app": False}]
                        },
                    },
                ]
            }
        }

        stacktrace = explainer.format_stacktrace(event)

        self.assertIn("Exception 1: TypeError", stacktrace)
        self.assertIn("First error", stacktrace)
        self.assertIn("Exception 2: ReferenceError", stacktrace)
        self.assertIn("Second error", stacktrace)

    async def test_format_stacktrace_without_stacktrace(self):
        explainer = self._create_explainer()

        event = {
            "properties": {
                "$exception_list": [
                    {
                        "type": "CustomError",
                        "value": "Something went wrong",
                        # No stacktrace key
                    }
                ]
            }
        }

        stacktrace = explainer.format_stacktrace(event)

        self.assertIn("Exception 1: CustomError", stacktrace)
        self.assertIn("Something went wrong", stacktrace)
        self.assertNotIn("Stack trace", stacktrace)

    async def test_format_stacktrace_with_empty_frames(self):
        explainer = self._create_explainer()

        event = {
            "properties": {
                "$exception_list": [
                    {
                        "type": "Error",
                        "value": "Test",
                        "stacktrace": {"frames": []},
                    }
                ]
            }
        }

        stacktrace = explainer.format_stacktrace(event)

        self.assertIn("Exception 1: Error", stacktrace)
        self.assertNotIn("Stack trace", stacktrace)
