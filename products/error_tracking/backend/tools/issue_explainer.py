import json

from pydantic import BaseModel, Field

from posthog.schema import DateRange, ErrorTrackingQuery

from posthog.hogql_queries.query_runner import get_query_runner
from posthog.models import Team, User

from products.error_tracking.backend.models import ErrorTrackingIssue
from products.error_tracking.backend.prompts import ERROR_TRACKING_EXPLAIN_ISSUE_PROMPT

from ee.hogai.llm import MaxChatAnthropic


class ErrorTrackingExplainIssueOutput(BaseModel):
    """Structured output for issue explanation."""

    generic_description: str = Field(description="A comprehensive technical explanation of the root cause")
    specific_problem: str = Field(description="A detailed summary of exactly how the issue occurs")
    possible_resolutions: list[str] = Field(
        default_factory=list,
        description="A list of potential solutions or mitigations to the issue",
        max_length=3,
    )


class IssueExplainer:
    """Shared logic for explaining error tracking issues.

    This class provides the core functionality for analyzing and explaining
    error tracking issues. It's used by both the contextual tool
    (ErrorTrackingExplainIssueTool) and the agent mode tool
    (ExplainErrorTrackingIssueTool).
    """

    def __init__(self, team: Team, user: User):
        self._team = team
        self._user = user

    def get_issue(self, issue_id: str) -> ErrorTrackingIssue | None:
        """Fetch the issue from the database."""
        try:
            return ErrorTrackingIssue.objects.get(id=issue_id, team=self._team)
        except ErrorTrackingIssue.DoesNotExist:
            return None

    def get_first_event(self, issue_id: str) -> dict | None:
        """Fetch the first event for the issue to get stack trace data."""
        query = ErrorTrackingQuery(
            kind="ErrorTrackingQuery",
            issueId=issue_id,
            dateRange=DateRange(date_from="all"),
            orderBy="first_seen",
            limit=1,
            volumeResolution=1,
            withAggregations=False,
            withFirstEvent=True,
            withLastEvent=False,
        )

        runner = get_query_runner(query, self._team)
        result = runner.calculate()

        if result.results and len(result.results) > 0:
            first_result = result.results[0]
            # Handle both dict and object access patterns
            if hasattr(first_result, "model_dump"):
                first_result = first_result.model_dump()
            if isinstance(first_result, dict):
                return first_result.get("first_event")
            return None

        return None

    def format_stacktrace(self, event: dict) -> str | None:
        """Format the exception list into a readable stack trace string."""
        if not event:
            return None

        properties = event.get("properties", {})
        # Properties may be a JSON string from ClickHouse
        if isinstance(properties, str):
            try:
                properties = json.loads(properties)
            except json.JSONDecodeError:
                return None
        exception_list = properties.get("$exception_list", [])

        if not exception_list:
            return None

        lines: list[str] = []

        for i, exception in enumerate(exception_list):
            exc_type = exception.get("type", "Unknown")
            exc_value = exception.get("value", "")

            lines.append(f"Exception {i + 1}: {exc_type}")
            if exc_value:
                lines.append(f"Message: {exc_value}")
            lines.append("")

            stacktrace = exception.get("stacktrace", {})
            frames = stacktrace.get("frames", []) if stacktrace else []

            if frames:
                lines.append("Stack trace (most recent call last):")
                # Reverse frames to show most recent first
                for frame in reversed(frames):
                    in_app = frame.get("in_app", False)
                    marker = "[IN-APP]" if in_app else ""

                    filename = frame.get("source", "unknown")
                    lineno = frame.get("line", "?")
                    colno = frame.get("column")
                    function = frame.get("resolved_name") or frame.get("mangled_name") or "<unknown>"

                    location = f"{filename}:{lineno}"
                    if colno:
                        location += f":{colno}"

                    lines.append(f"  {marker} at {function} ({location})")

                    # Include context lines if available
                    context_line = frame.get("context_line")
                    if context_line:
                        lines.append(f"       > {context_line.strip()}")

                lines.append("")

        return "\n".join(lines) if lines else None

    async def analyze_issue(self, stacktrace: str) -> ErrorTrackingExplainIssueOutput:
        """Analyze the issue using LLM."""
        formatted_prompt = ERROR_TRACKING_EXPLAIN_ISSUE_PROMPT.replace("{{{stacktrace}}}", stacktrace)

        llm = MaxChatAnthropic(
            user=self._user,
            team=self._team,
            model="claude-sonnet-4-5",
            temperature=0.1,
        ).with_structured_output(ErrorTrackingExplainIssueOutput)

        analysis_result = await llm.ainvoke([{"role": "user", "content": formatted_prompt}])

        if isinstance(analysis_result, dict):
            return ErrorTrackingExplainIssueOutput(**analysis_result)
        # with_structured_output returns ErrorTrackingExplainIssueOutput but typed as BaseModel
        return analysis_result  # type: ignore[return-value]

    def format_explanation(self, analysis: ErrorTrackingExplainIssueOutput, issue_name: str) -> str:
        """Format the analysis into a user-friendly explanation."""
        lines = []
        lines.append(f"### Issue: {issue_name}")
        lines.append("")
        lines.append(analysis.generic_description)

        lines.append("")
        lines.append("#### What's happening?")
        lines.append(analysis.specific_problem)

        lines.append("")
        lines.append("#### How to fix it:")
        for i, resolution in enumerate(analysis.possible_resolutions, 1):
            lines.append(f"{i}. {resolution}")

        return "\n".join(lines)
