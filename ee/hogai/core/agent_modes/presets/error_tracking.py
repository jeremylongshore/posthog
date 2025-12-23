from typing import TYPE_CHECKING

from posthog.schema import AgentMode

from ee.hogai.core.agent_modes.factory import AgentModeDefinition
from ee.hogai.core.agent_modes.toolkit import AgentToolkit
from ee.hogai.tools.todo_write import TodoWriteExample

if TYPE_CHECKING:
    from ee.hogai.tool import MaxTool


POSITIVE_EXAMPLE_SEARCH_ERRORS = """
User: Show me the most frequent errors from the last week
Assistant: I'll search for the most frequent error tracking issues from the past week.
*Uses search_error_tracking_issues with orderBy: "occurrences" and dateRange: { date_from: "-7d" }*
""".strip()

POSITIVE_EXAMPLE_SEARCH_ERRORS_REASONING = """
The assistant used the search tool because:
1. The user wants to find errors based on frequency criteria
2. The search_error_tracking_issues tool can filter by date range and order by occurrences
3. This is a straightforward search that doesn't require multiple steps
""".strip()

POSITIVE_EXAMPLE_SEARCH_AND_EXPLAIN = """
User: What's causing our most frequent error?
Assistant: I'll search for the most frequent error and then explain what's causing it.
*Creates todo list with the following items:*
1. Search for the most frequent error tracking issue
2. Explain the root cause and provide solutions
*Uses search_error_tracking_issues with orderBy: "occurrences" and limit: 1*
After getting the issue, the assistant uses explain_error_tracking_issue with the issue_id to analyze the stack trace and provide a detailed explanation.
""".strip()

POSITIVE_EXAMPLE_SEARCH_AND_EXPLAIN_REASONING = """
The assistant used the todo list because:
1. The user wants to understand the root cause, not just see a list
2. This requires two steps: first find the issue, then explain it
3. The explain_error_tracking_issue tool needs an issue_id from the search results
4. Breaking this into steps ensures the assistant gets the issue_id before explaining
""".strip()


class ErrorTrackingAgentToolkit(AgentToolkit):
    POSITIVE_TODO_EXAMPLES = [
        TodoWriteExample(
            example=POSITIVE_EXAMPLE_SEARCH_ERRORS,
            reasoning=POSITIVE_EXAMPLE_SEARCH_ERRORS_REASONING,
        ),
        TodoWriteExample(
            example=POSITIVE_EXAMPLE_SEARCH_AND_EXPLAIN,
            reasoning=POSITIVE_EXAMPLE_SEARCH_AND_EXPLAIN_REASONING,
        ),
    ]

    @property
    def tools(self) -> list[type["MaxTool"]]:
        from products.error_tracking.backend.tools.explain_issue import ExplainErrorTrackingIssueTool
        from products.error_tracking.backend.tools.search_issues import SearchErrorTrackingIssuesTool

        tools: list[type[MaxTool]] = [SearchErrorTrackingIssuesTool, ExplainErrorTrackingIssueTool]
        return tools


error_tracking_agent = AgentModeDefinition(
    mode=AgentMode.ERROR_TRACKING,
    mode_description="Specialized mode for analyzing error tracking issues. This mode allows you to search and filter error tracking issues by status, date range, frequency, and other criteria.",
    toolkit_class=ErrorTrackingAgentToolkit,
)
