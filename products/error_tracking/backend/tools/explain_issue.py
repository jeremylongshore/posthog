from textwrap import dedent
from typing import Literal

import structlog
from posthoganalytics import capture_exception
from pydantic import BaseModel, Field

from posthog.sync import database_sync_to_async

from ee.hogai.tool import MaxTool, ToolMessagesArtifact

from .issue_explainer import ErrorTrackingExplainIssueOutput, IssueExplainer

logger = structlog.get_logger(__name__)


class ExplainErrorTrackingIssueArgs(BaseModel):
    issue_id: str = Field(
        description=(
            "The UUID of the error tracking issue (from the 'ID' field in search_error_tracking_issues results). "
            "Must be a valid UUID format like '01234567-89ab-cdef-0123-456789abcdef', NOT the error name."
        ),
    )


class ExplainErrorTrackingIssueTool(MaxTool):
    name: Literal["explain_error_tracking_issue"] = "explain_error_tracking_issue"
    args_schema: type[BaseModel] = ExplainErrorTrackingIssueArgs
    description: str = dedent("""
        Analyze and explain an error tracking issue in detail.

        # When to use this tool:
        - User asks for an explanation of a specific error/issue
        - User wants to understand what's causing an error
        - User asks "why is this happening?" about an issue
        - User wants help debugging or understanding an issue they found

        # What this tool returns:
        A detailed analysis including:
        - Root cause explanation
        - Technical walkthrough of how the issue occurs
        - Potential solutions or mitigations

        # Prerequisites:
        - You MUST first use the search_error_tracking_issues tool to find issues
        - Use the UUID from the 'ID' field in search results (e.g., '01234567-89ab-cdef-0123-456789abcdef')
        - Do NOT use the error name as the issue_id
        """).strip()

    async def _arun_impl(self, issue_id: str) -> tuple[str, ToolMessagesArtifact | None]:
        try:
            explainer = IssueExplainer(team=self._team, user=self._user)

            # Fetch the issue from the database
            issue = await database_sync_to_async(explainer.get_issue, thread_sensitive=False)(issue_id)
            if issue is None:
                return f"Issue with ID '{issue_id}' not found.", None

            # Fetch the first event with stack trace
            first_event = await database_sync_to_async(explainer.get_first_event, thread_sensitive=False)(issue_id)
            if first_event is None:
                return (
                    f"No events found for issue '{issue.name or issue_id}'. Cannot analyze without stack trace data.",
                    None,
                )

            # Extract and format the stack trace
            stacktrace = explainer.format_stacktrace(first_event)
            if not stacktrace:
                return (
                    f"No stack trace available for issue '{issue.name or issue_id}'. Cannot analyze without stack trace data.",
                    None,
                )

            # Analyze the issue using LLM
            analysis = await explainer.analyze_issue(stacktrace)

            # Format the explanation for the user
            formatted = explainer.format_explanation(analysis, issue.name or "Unnamed issue")

            return formatted, None

        except Exception as e:
            capture_exception(e)
            logger.exception("Error explaining error tracking issue", error=str(e))
            return f"Error analyzing issue: {e}", None


# Re-export for backwards compatibility
__all__ = ["ExplainErrorTrackingIssueTool", "ExplainErrorTrackingIssueArgs", "ErrorTrackingExplainIssueOutput"]
