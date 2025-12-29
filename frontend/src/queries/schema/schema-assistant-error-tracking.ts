/**
 * Schema types for Max AI error tracking tools
 */

export interface MaxErrorTrackingFilters {
    /** Issue status filter (active, resolved, etc.) */
    status?: string | null
    /** Free text search query */
    search_query?: string | null
    /** Start of date range */
    date_from?: string | null
    /** End of date range */
    date_to?: string | null
    /** Field to order by */
    order_by?: string | null
    /** Order direction (ASC or DESC) */
    order_direction?: string | null
    /** Number of results to return */
    limit?: number | null
    /** Whether there are more results available */
    has_more?: boolean | null
    /** Cursor for pagination */
    next_cursor?: string | null
}
