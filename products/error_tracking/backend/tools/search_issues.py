import json
from datetime import UTC
from textwrap import dedent
from typing import Literal

from django.utils import timezone

import structlog
from posthoganalytics import capture_exception
from pydantic import BaseModel, Field

from posthog.schema import ErrorTrackingQuery, MaxErrorTrackingFilters, Status2

from ee.hogai.context.insight.query_executor import AssistantQueryExecutor
from ee.hogai.tool import MaxTool

logger = structlog.get_logger(__name__)


SEARCH_QUERY_EXAMPLES = """
# Examples

## Status filtering
- "Show me active errors" → status: "active"
- "What resolved issues do we have?" → status: "resolved"
- "Show suppressed errors" → status: "suppressed"

## Text search
- "TypeError errors" → searchQuery: "TypeError"
- "Errors mentioning 'undefined'" → searchQuery: "undefined"
- "Find null pointer exceptions" → searchQuery: "null pointer"

## Date range
- "Errors from last 7 days" → dateRange: { date_from: "-7d" }
- "Issues since last week" → dateRange: { date_from: "-7d" }
- "Errors from December" → dateRange: { date_from: "2024-12-01", date_to: "2024-12-31" }

## Ordering
- "Most frequent errors" → orderBy: "occurrences", orderDirection: "DESC"
- "Newest errors first" → orderBy: "first_seen", orderDirection: "DESC"
- "Most recent errors" → orderBy: "last_seen", orderDirection: "DESC"
- "Errors affecting most users" → orderBy: "users", orderDirection: "DESC"

## Combined queries
- "Active TypeError errors from last week" → status: "active", searchQuery: "TypeError", dateRange: { date_from: "-7d" }
- "Top 10 most frequent resolved issues" → status: "resolved", orderBy: "occurrences", limit: 10
"""


class SearchErrorTrackingIssuesArgs(BaseModel):
    query: ErrorTrackingQuery = Field(
        description=dedent(f"""
        User's question converted into an error tracking query.

        IMPORTANT: When the user asks to "show more" or "next page", you should pass the cursor
        from the previous response to continue pagination. Do NOT modify the query parameters.""").strip()
        + dedent(f"""

        # Query Structure

        ## status (optional)
        Filter by issue status:
        - "active": Currently active issues (default if not specified)
        - "resolved": Issues marked as resolved
        - "pending_release": Issues pending a release
        - "suppressed": Suppressed/muted issues
        - "archived": Archived issues
        - "all": Show all issues regardless of status

        ## searchQuery (optional)
        Free text search across:
        - Exception type (e.g., "TypeError", "ReferenceError")
        - Exception message
        - Function names in stack traces
        - File paths in stack traces

        ## dateRange (REQUIRED)
        Time range for the query:
        - date_from: Start date (relative like "-7d", "-30d" or absolute "2024-12-01")
        - date_to: End date (null for "until now", or absolute date)

        Common relative formats:
        - "-7d" = last 7 days
        - "-30d" = last 30 days
        - "-24h" = last 24 hours

        ## orderBy (REQUIRED)
        Sort results by:
        - "last_seen": When the issue was last seen (most recent activity)
        - "first_seen": When the issue first appeared
        - "occurrences": Total occurrence count
        - "users": Number of affected users
        - "sessions": Number of affected sessions
        - "revenue": Revenue impact (if configured)

        ## orderDirection (optional)
        - "DESC": Descending (default, highest/newest first)
        - "ASC": Ascending (lowest/oldest first)

        ## limit (optional)
        Number of results to return (1-100, default 25)

        ## filterGroup (optional)
        Property filters for advanced filtering. Structure:
        {{
            "type": "AND" | "OR",
            "values": [
                {{
                    "type": "AND" | "OR",
                    "values": [
                        {{
                            "type": "event" | "person" | "session",
                            "key": "property_name",
                            "value": "value_or_array",
                            "operator": "exact" | "icontains" | "is_set" | ...
                        }}
                    ]
                }}
            ]
        }}

        Common filter properties:
        - event.$browser: Browser type
        - event.$os: Operating system
        - event.$device_type: Device type (Desktop, Mobile, Tablet)
        - event.$current_url: URL where error occurred
        - event.$lib: SDK/library used (web, posthog-python, posthog-node, etc.)

        ## filterTestAccounts (optional)
        - true: Exclude internal/test accounts
        - false: Include all accounts

        ## volumeResolution (REQUIRED)
        Resolution for volume chart data. Use 1 for daily buckets.

        {SEARCH_QUERY_EXAMPLES}
        """).strip()
    )
    cursor: str | None = Field(
        default=None,
        description="Pagination cursor from previous search results. Pass this to get the next page of results.",
    )


class SearchErrorTrackingIssuesTool(MaxTool):
    name: Literal["search_error_tracking_issues"] = "search_error_tracking_issues"
    args_schema: type[BaseModel] = SearchErrorTrackingIssuesArgs
    description: str = dedent("""
        Search for error tracking issues based on criteria like status, search text, date range, and ordering.

        # When to use this tool:
        - User asks to find, search, or list error tracking issues
        - User asks about errors, exceptions, or issues in their application
        - User wants to filter errors by status, date, frequency, or other criteria
        - User asks questions like "show me recent errors" or "what are the most common issues"
        - User asks to "show more" or see "next page" of results (use the cursor from previous results)

        # What this tool returns:
        A formatted list of matching issues with their name, status, occurrence count, and other key metrics.
        If more results are available, a cursor will be provided for pagination.
        """).strip()

    async def _arun_impl(
        self, query: ErrorTrackingQuery, cursor: str | None = None
    ) -> tuple[str, MaxErrorTrackingFilters | None]:
        # Ensure reasonable defaults
        if query.limit is None or query.limit <= 0:
            query.limit = 25
        elif query.limit > 100:
            query.limit = 100

        # Default to active issues if no status specified
        if query.status is None:
            query.status = Status2.ACTIVE

        # Apply cursor offset for pagination
        current_offset = 0
        if cursor:
            try:
                current_offset = int(cursor)
                query.offset = current_offset
            except ValueError:
                logger.warning("Invalid pagination cursor", cursor=cursor)

        try:
            utc_now = timezone.now().astimezone(UTC)
            executor = AssistantQueryExecutor(self._team, utc_now)
            query_results = await executor.aexecute_query(query)
        except Exception as e:
            capture_exception(e)
            logger.exception("Error executing error tracking query", error=str(e))
            return f"Error searching for issues: {e}", None

        # Handle both pydantic model and dict responses
        results: list = []
        if hasattr(query_results, "results"):
            results = query_results.results
        elif isinstance(query_results, dict):
            results = query_results.get("results", [])

        has_more = len(results) >= query.limit
        next_cursor = str(current_offset + query.limit) if has_more else None

        content = self._format_results(results, has_more)

        filters = MaxErrorTrackingFilters(
            status=query.status,
            search_query=query.searchQuery,
            date_from=query.dateRange.date_from if query.dateRange else None,
            date_to=query.dateRange.date_to if query.dateRange else None,
            order_by=query.orderBy,
            order_direction=query.orderDirection,
            limit=query.limit,
            has_more=has_more,
            next_cursor=next_cursor,
        )

        return content, filters

    def _format_results(self, results: list, has_more: bool = False) -> str:
        """Format query results as text output."""

        if not results:
            return "No issues found matching your criteria."

        total_count = len(results)
        if total_count == 1:
            content = "Found 1 issue matching your criteria:\n\n"
        else:
            content = f"Found {total_count} issues matching your criteria:\n\n"

        # Show up to 10 issues in the response
        for i, issue in enumerate(results[:10], 1):
            content += self._format_issue(i, issue)

        if total_count > 10:
            content += f"\n...and {total_count - 10} more issues in this batch"

        if has_more:
            content += "\n\nMore issues are available. Ask me to show more if needed."

        return content

    def _format_issue(self, index: int, issue) -> str:
        """Format a single issue for display."""
        if hasattr(issue, "model_dump"):
            issue = issue.model_dump()

        issue_id = issue.get("id", "")
        name = issue.get("name") or "Unnamed issue"
        description = issue.get("description") or self._extract_exception_message(issue)
        status = issue.get("status", "unknown")
        first_seen = issue.get("first_seen", "")
        last_seen = issue.get("last_seen", "")

        aggregations = issue.get("aggregations", {})
        if aggregations:
            occurrences = int(aggregations.get("occurrences", 0))
            users = int(aggregations.get("users", 0))
            sessions = int(aggregations.get("sessions", 0))
        else:
            occurrences = 0
            users = 0
            sessions = 0

        first_seen_str = self._format_date(first_seen)
        last_seen_str = self._format_date(last_seen)

        lines = [f"{index}. {name}"]
        if description:
            # Truncate long descriptions
            desc_display = description[:100] + "..." if len(description) > 100 else description
            lines.append(f"   {desc_display}")
        lines.append(f"   ID: {issue_id}")
        lines.append(f"   Status: {status} | Occurrences: {occurrences:,} | Users: {users:,} | Sessions: {sessions:,}")

        if first_seen_str or last_seen_str:
            date_parts = []
            if first_seen_str:
                date_parts.append(f"First seen: {first_seen_str}")
            if last_seen_str:
                date_parts.append(f"Last seen: {last_seen_str}")
            lines.append(f"   {' | '.join(date_parts)}")

        # Empty line between issues for formatting
        lines.append("")
        return "\n".join(lines)

    def _extract_exception_message(self, issue: dict) -> str | None:
        """Extract exception message from first_event properties."""
        first_event = issue.get("first_event")
        if not first_event:
            return None

        properties = first_event.get("properties")
        if not properties:
            return None

        try:
            if isinstance(properties, str):
                props = json.loads(properties)
            else:
                props = properties

            exception_list = props.get("$exception_list", [])
            if exception_list and len(exception_list) > 0:
                first_exception = exception_list[0]
                value = first_exception.get("value")
                if value:
                    return value
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

        return None

    def _format_date(self, date_value) -> str:
        """Format a date value for display."""
        if not date_value:
            return ""
        from datetime import datetime

        try:
            if isinstance(date_value, datetime):
                return date_value.strftime("%Y-%m-%d %H:%M")
            elif isinstance(date_value, str):
                dt = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, AttributeError):
            return str(date_value) if date_value else ""
        return str(date_value)
