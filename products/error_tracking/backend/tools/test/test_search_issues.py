from freezegun import freeze_time
from posthog.test.base import (
    ClickhouseTestMixin,
    NonAtomicBaseTest,
    _create_event,
    _create_person,
    flush_persons_and_events,
)
from unittest.mock import patch

from django.utils.timezone import now

from dateutil.relativedelta import relativedelta
from langchain_core.runnables import RunnableConfig
from parameterized import parameterized

from posthog.schema import DateRange, ErrorTrackingQuery, MaxErrorTrackingFilters

from products.error_tracking.backend.models import ErrorTrackingIssue, ErrorTrackingIssueFingerprintV2
from products.error_tracking.backend.tools.search_issues import SearchErrorTrackingIssuesTool

from ee.hogai.context.context import AssistantContextManager
from ee.hogai.utils.types import AssistantState
from ee.hogai.utils.types.base import NodePath


@freeze_time("2025-01-15T12:00:00Z")
class TestSearchErrorTrackingIssuesTool(ClickhouseTestMixin, NonAtomicBaseTest):
    CLASS_DATA_LEVEL_SETUP = False

    distinct_id_one = "user_1"
    distinct_id_two = "user_2"
    issue_id_one = "01936e7f-d7ff-7314-b2d4-7627981e34f0"
    issue_id_two = "01936e80-5e69-7e70-b837-871f5cdad28b"
    issue_id_three = "01936e80-aa51-746f-aec4-cdf16a5c5332"

    def setUp(self):
        super().setUp()
        self.tool_call_id = "test_tool_call_id"

        _create_person(
            team=self.team,
            distinct_ids=[self.distinct_id_one],
            is_identified=True,
        )
        _create_person(
            team=self.team,
            properties={"email": "email@posthog.com"},
            distinct_ids=[self.distinct_id_two],
            is_identified=True,
        )

        self.create_events_and_issue(
            issue_id=self.issue_id_one,
            issue_name="TypeError: Cannot read property 'map' of undefined",
            fingerprint="issue_one_fingerprint",
            distinct_ids=[self.distinct_id_one, self.distinct_id_two],
            timestamp=now() - relativedelta(hours=3),
        )
        self.create_events_and_issue(
            issue_id=self.issue_id_two,
            issue_name="ReferenceError: foo is not defined",
            fingerprint="issue_two_fingerprint",
            distinct_ids=[self.distinct_id_one],
            timestamp=now() - relativedelta(hours=2),
        )
        self.create_events_and_issue(
            issue_id=self.issue_id_three,
            issue_name="SyntaxError: Unexpected token",
            fingerprint="issue_three_fingerprint",
            distinct_ids=[self.distinct_id_two],
            timestamp=now() - relativedelta(hours=1),
            status=ErrorTrackingIssue.Status.RESOLVED,
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
    ):
        if timestamp:
            with freeze_time(timestamp):
                self.create_issue(issue_id, fingerprint, name=issue_name, status=status)
        else:
            self.create_issue(issue_id, fingerprint, name=issue_name, status=status)

        event_properties = {"$exception_issue_id": issue_id, "$exception_fingerprint": fingerprint}

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

        tool = await SearchErrorTrackingIssuesTool.create_tool_class(
            team=self.team,
            user=self.user,
            state=state,
            config=config,
            context_manager=context_manager,
            node_path=(NodePath(name="test_node", tool_call_id=self.tool_call_id, message_id="test"),),
        )
        return tool

    def _create_query(
        self,
        date_from="-7d",
        date_to=None,
        status=None,
        search_query=None,
        order_by="last_seen",
        limit=25,
    ) -> ErrorTrackingQuery:
        return ErrorTrackingQuery(
            kind="ErrorTrackingQuery",
            dateRange=DateRange(date_from=date_from, date_to=date_to),
            status=status,
            searchQuery=search_query,
            orderBy=order_by,
            limit=limit,
            volumeResolution=1,
            withAggregations=True,
        )

    async def test_returns_no_issues_message_when_none_found(self):
        tool = await self._create_tool()
        query = self._create_query(search_query="nonexistent_error_xyz")

        result_text, filters = await tool._arun_impl(query=query)

        self.assertIn("No issues found", result_text)
        self.assertIsInstance(filters, MaxErrorTrackingFilters)
        self.assertEqual(filters.search_query, "nonexistent_error_xyz")
        self.assertFalse(filters.has_more)

    async def test_returns_active_issues_by_default(self):
        tool = await self._create_tool()
        query = self._create_query(status="active")

        result_text, filters = await tool._arun_impl(query=query)

        self.assertIn("Found 2 issues", result_text)
        self.assertIn("TypeError", result_text)
        self.assertIn("ReferenceError", result_text)
        self.assertNotIn("SyntaxError", result_text)
        self.assertIsInstance(filters, MaxErrorTrackingFilters)
        self.assertEqual(filters.status, "active")

    async def test_returns_resolved_issues_when_filtered(self):
        tool = await self._create_tool()
        query = self._create_query(status="resolved")

        result_text, filters = await tool._arun_impl(query=query)

        self.assertIn("Found 1 issue", result_text)
        self.assertIn("SyntaxError", result_text)
        self.assertNotIn("TypeError", result_text)
        self.assertIsInstance(filters, MaxErrorTrackingFilters)
        self.assertEqual(filters.status, "resolved")

    async def test_returns_all_issues_when_status_all(self):
        tool = await self._create_tool()
        query = self._create_query(status="all")

        result_text, filters = await tool._arun_impl(query=query)

        self.assertIn("Found 3 issues", result_text)
        self.assertIn("TypeError", result_text)
        self.assertIn("ReferenceError", result_text)
        self.assertIn("SyntaxError", result_text)
        self.assertIsInstance(filters, MaxErrorTrackingFilters)
        self.assertEqual(filters.status, "all")

    @patch("ee.hogai.context.insight.query_executor.process_query_dict")
    async def test_search_query_filters_by_text(self, mock_process_query):
        mock_process_query.return_value = {
            "results": [
                {
                    "name": "TypeError: Cannot read property 'map' of undefined",
                    "status": "active",
                    "aggregations": {"occurrences": 1, "users": 1, "sessions": 0},
                }
            ]
        }

        tool = await self._create_tool()
        query = self._create_query(status="all", search_query="TypeError")

        result_text, filters = await tool._arun_impl(query=query)

        self.assertIn("Found 1 issue", result_text)
        self.assertIn("TypeError", result_text)
        self.assertIsInstance(filters, MaxErrorTrackingFilters)
        self.assertEqual(filters.search_query, "TypeError")
        # Verify search query was passed
        call_args = mock_process_query.call_args
        query_dict = call_args[0][1]
        self.assertEqual(query_dict["searchQuery"], "TypeError")

    @patch("ee.hogai.context.insight.query_executor.process_query_dict")
    async def test_respects_limit(self, mock_process_query):
        mock_process_query.return_value = {
            "results": [
                {"name": "Error 1", "status": "active", "aggregations": {"occurrences": 1, "users": 1, "sessions": 0}},
                {"name": "Error 2", "status": "active", "aggregations": {"occurrences": 2, "users": 1, "sessions": 0}},
            ]
        }

        tool = await self._create_tool()
        query = self._create_query(status="all", limit=2)

        result_text, filters = await tool._arun_impl(query=query)

        self.assertIn("Found 2 issues", result_text)
        self.assertIsInstance(filters, MaxErrorTrackingFilters)
        self.assertEqual(filters.limit, 2)
        self.assertTrue(filters.has_more)
        self.assertIsNotNone(filters.next_cursor)
        # Cursor should be offset 0 + limit 2 = "2"
        self.assertEqual(filters.next_cursor, "2")
        # Verify limit was passed to the query
        call_args = mock_process_query.call_args
        query_dict = call_args[0][1]
        self.assertEqual(query_dict["limit"], 2)

    @patch("ee.hogai.context.insight.query_executor.process_query_dict")
    async def test_cursor_pagination_returns_next_page(self, mock_process_query):
        # First call returns 2 results (at limit), second call returns 1 result
        mock_process_query.side_effect = [
            {
                "results": [
                    {
                        "name": "Error 1",
                        "status": "active",
                        "aggregations": {"occurrences": 1, "users": 1, "sessions": 0},
                    },
                    {
                        "name": "Error 2",
                        "status": "active",
                        "aggregations": {"occurrences": 2, "users": 1, "sessions": 0},
                    },
                ]
            },
            {
                "results": [
                    {
                        "name": "Error 3",
                        "status": "active",
                        "aggregations": {"occurrences": 3, "users": 1, "sessions": 0},
                    },
                ]
            },
        ]

        tool = await self._create_tool()
        query = self._create_query(status="all", limit=2)

        # First page
        result_text_1, filters_1 = await tool._arun_impl(query=query)
        self.assertIn("Found 2 issues", result_text_1)
        self.assertTrue(filters_1.has_more)
        self.assertEqual(filters_1.next_cursor, "2")

        # Second page using cursor
        result_text_2, filters_2 = await tool._arun_impl(query=query, cursor=filters_1.next_cursor)
        self.assertIn("Found 1 issue", result_text_2)
        self.assertFalse(filters_2.has_more)
        self.assertIsNone(filters_2.next_cursor)

    async def test_formats_issue_with_aggregations(self):
        tool = await self._create_tool()
        query = self._create_query(status="active")

        result_text, filters = await tool._arun_impl(query=query)

        self.assertIn("Status:", result_text)
        self.assertIn("Occurrences:", result_text)
        self.assertIn("Users:", result_text)
        self.assertIn("Sessions:", result_text)
        self.assertIsInstance(filters, MaxErrorTrackingFilters)

    async def test_limits_excessive_limit_to_100(self):
        tool = await self._create_tool()
        query = self._create_query(status="all", limit=500)

        result_text, filters = await tool._arun_impl(query=query)

        # Should cap at 100, but since we only have 3 issues, should return 3
        self.assertIn("Found 3 issues", result_text)
        self.assertIsInstance(filters, MaxErrorTrackingFilters)
        self.assertEqual(filters.limit, 100)

    async def test_defaults_to_25_when_no_limit(self):
        tool = await self._create_tool()
        query = self._create_query(status="all")
        query.limit = None  # Explicitly set to None

        result_text, filters = await tool._arun_impl(query=query)

        # Should default to 25, but since we only have 3 issues, should return 3
        self.assertIn("Found 3 issues", result_text)
        self.assertIsInstance(filters, MaxErrorTrackingFilters)
        self.assertEqual(filters.limit, 25)


class TestSearchErrorTrackingIssuesToolFormatting(NonAtomicBaseTest):
    async def _create_tool(self):
        config: RunnableConfig = RunnableConfig()
        context_manager = AssistantContextManager(team=self.team, user=self.user, config=config)

        return await SearchErrorTrackingIssuesTool.create_tool_class(
            team=self.team,
            user=self.user,
            state=AssistantState(messages=[]),
            config=config,
            context_manager=context_manager,
            node_path=(NodePath(name="test_node", tool_call_id="test", message_id="test"),),
        )

    async def test_format_issue_with_all_fields(self):
        tool = await self._create_tool()
        issue = {
            "id": "01234567-89ab-cdef-0123-456789abcdef",
            "name": "TypeError: Cannot read 'undefined'",
            "status": "active",
            "first_seen": "2025-01-10T10:00:00Z",
            "last_seen": "2025-01-15T11:00:00Z",
            "aggregations": {
                "occurrences": 150,
                "users": 25,
                "sessions": 30,
            },
        }

        result = tool._format_issue(1, issue)

        self.assertIn("1. TypeError: Cannot read 'undefined'", result)
        self.assertIn("ID: 01234567-89ab-cdef-0123-456789abcdef", result)
        self.assertIn("Status: active", result)
        self.assertIn("Occurrences: 150", result)
        self.assertIn("Users: 25", result)
        self.assertIn("Sessions: 30", result)
        self.assertIn("First seen:", result)
        self.assertIn("Last seen:", result)

    async def test_format_issue_with_minimal_fields(self):
        tool = await self._create_tool()
        issue = {
            "id": "abcd1234-5678-90ab-cdef-1234567890ab",
            "status": "active",
        }

        result = tool._format_issue(1, issue)

        self.assertIn("1. Unnamed issue", result)
        self.assertIn("ID: abcd1234-5678-90ab-cdef-1234567890ab", result)
        self.assertIn("Status: active", result)

    async def test_format_results_empty(self):
        tool = await self._create_tool()

        result = tool._format_results([])

        self.assertIn("No issues found", result)

    async def test_format_results_single(self):
        tool = await self._create_tool()

        results = [{"name": "Error", "status": "active", "aggregations": {"occurrences": 1, "users": 1, "sessions": 0}}]

        result = tool._format_results(results)

        self.assertIn("Found 1 issue", result)

    async def test_format_results_multiple(self):
        tool = await self._create_tool()

        results = [
            {"name": "Error 1", "status": "active", "aggregations": {"occurrences": 1, "users": 1, "sessions": 0}},
            {"name": "Error 2", "status": "active", "aggregations": {"occurrences": 2, "users": 2, "sessions": 1}},
        ]

        result = tool._format_results(results)

        self.assertIn("Found 2 issues", result)
        self.assertIn("1. Error 1", result)
        self.assertIn("2. Error 2", result)

    async def test_format_results_limits_to_10(self):
        tool = await self._create_tool()

        results = [
            {
                "name": f"Error {i}",
                "status": "active",
                "aggregations": {"occurrences": i, "users": i, "sessions": 0},
            }
            for i in range(15)
        ]

        result = tool._format_results(results)

        self.assertIn("Found 15 issues", result)
        self.assertIn("...and 5 more issues in this batch", result)
        self.assertIn("1. Error 0", result)
        self.assertIn("10. Error 9", result)
        self.assertNotIn("11.", result)

    async def test_format_results_with_pagination(self):
        tool = await self._create_tool()

        results = [
            {"name": "Error 1", "status": "active", "aggregations": {"occurrences": 1, "users": 1, "sessions": 0}},
        ]

        result = tool._format_results(results, has_more=True)

        self.assertIn("More issues are available", result)

    @parameterized.expand(
        [
            ("2025-01-15T10:30:00Z", "2025-01-15 10:30"),
            ("2025-01-15T10:30:00+00:00", "2025-01-15 10:30"),
            ("", ""),
            (None, ""),
        ]
    )
    async def test_format_date_handles_various_formats(self, date_value, expected):
        tool = await self._create_tool()
        result = tool._format_date(date_value)
        self.assertEqual(result, expected)
