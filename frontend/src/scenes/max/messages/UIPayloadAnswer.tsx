import { BindLogic, useActions, useValues } from 'kea'
import { router } from 'kea-router'

import { LemonButton, LemonTag, Spinner } from '@posthog/lemon-ui'

import { EmptyMessage } from 'lib/components/EmptyMessage/EmptyMessage'
import { sessionPlayerModalLogic } from 'scenes/session-recordings/player/modal/sessionPlayerModalLogic'
import { SessionRecordingPreview } from 'scenes/session-recordings/playlist/SessionRecordingPreview'
import {
    SessionRecordingPlaylistLogicProps,
    sessionRecordingsPlaylistLogic,
} from 'scenes/session-recordings/playlist/sessionRecordingsPlaylistLogic'
import { urls } from 'scenes/urls'

import { MaxErrorTrackingFilters } from '~/queries/schema/schema-assistant-error-tracking'
import { AssistantTool } from '~/queries/schema/schema-assistant-messages'
import { RecordingUniversalFilters } from '~/types'

import { MessageTemplate } from './MessageTemplate'
import { RecordingsFiltersSummary } from './RecordingsFiltersSummary'

export const RENDERABLE_UI_PAYLOAD_TOOLS: AssistantTool[] = [
    'search_session_recordings',
    'search_error_tracking_issues',
    'create_form',
]

export function UIPayloadAnswer({
    toolCallId,
    toolName,
    toolPayload,
}: {
    toolCallId: string
    toolName: string
    toolPayload: any
}): JSX.Element | null {
    if (toolName === 'search_session_recordings') {
        const filters = toolPayload as RecordingUniversalFilters
        return <RecordingsWidget toolCallId={toolCallId} filters={filters} />
    }
    if (toolName === 'search_error_tracking_issues') {
        const filters = toolPayload as MaxErrorTrackingFilters
        return <ErrorTrackingFiltersWidget filters={filters} />
    }
    // It's not expected to hit the null branch below, because such a case SHOULD have already been filtered out
    // in maxThreadLogic.selectors.threadGrouped, but better safe than sorry - there can be deployments mismatches etc.
    return null
}

export function RecordingsWidget({
    toolCallId,
    filters,
}: {
    toolCallId: string
    filters: RecordingUniversalFilters
}): JSX.Element {
    const logicProps: SessionRecordingPlaylistLogicProps = {
        logicKey: `ai-recordings-widget-${toolCallId}`,
        filters,
        updateSearchParams: false,
        autoPlay: false,
    }

    return (
        <BindLogic logic={sessionRecordingsPlaylistLogic} props={logicProps}>
            <MessageTemplate type="ai" wrapperClassName="w-full" boxClassName="p-0 overflow-hidden">
                <RecordingsFiltersSummary filters={filters} />
                <RecordingsListContent />
            </MessageTemplate>
        </BindLogic>
    )
}

function RecordingsListContent(): JSX.Element {
    const { otherRecordings, sessionRecordingsResponseLoading, hasNext } = useValues(sessionRecordingsPlaylistLogic)
    const { maybeLoadSessionRecordings } = useActions(sessionRecordingsPlaylistLogic)
    const { openSessionPlayer } = useActions(sessionPlayerModalLogic())

    const hasRecordings = otherRecordings.length > 0

    return (
        <div className="border-t *:not-first:border-t max-h-80 overflow-y-auto">
            {sessionRecordingsResponseLoading && !hasRecordings ? (
                <div className="flex items-center justify-center gap-2 py-12 text-muted">
                    <Spinner textColored />
                    <span>Loading recordings...</span>
                </div>
            ) : !hasRecordings ? (
                <div className="py-2">
                    <EmptyMessage title="No recordings found" description="No recordings match the specified filters" />
                </div>
            ) : (
                <>
                    {otherRecordings.map((recording) => (
                        <div
                            key={recording.id}
                            onClick={(e) => {
                                e.preventDefault()
                                openSessionPlayer(recording)
                            }}
                        >
                            <SessionRecordingPreview recording={recording} selectable={false} />
                        </div>
                    ))}
                    {hasNext && (
                        <div className="p-2">
                            <LemonButton
                                fullWidth
                                type="secondary"
                                size="small"
                                onClick={() => maybeLoadSessionRecordings('older')}
                                loading={sessionRecordingsResponseLoading}
                            >
                                Load more recordings
                            </LemonButton>
                        </div>
                    )}
                </>
            )}
        </div>
    )
}

export function ErrorTrackingFiltersWidget({ filters }: { filters: MaxErrorTrackingFilters }): JSX.Element {
    const { push } = useActions(router)

    const handleApplyFilters = (): void => {
        const params = new URLSearchParams()
        if (filters.status) {
            params.set('status', filters.status)
        }
        if (filters.search_query) {
            params.set('searchQuery', filters.search_query)
        }
        if (filters.date_from) {
            params.set('dateFrom', filters.date_from)
        }
        if (filters.date_to) {
            params.set('dateTo', filters.date_to)
        }
        const query = params.toString()
        push(urls.errorTracking() + (query ? `?${query}` : ''))
    }

    return (
        <MessageTemplate type="ai" boxClassName="p-3">
            <div className="flex flex-wrap gap-2 mb-3">
                {filters.status && <LemonTag>Status: {filters.status}</LemonTag>}
                {filters.search_query && <LemonTag>Search: {filters.search_query}</LemonTag>}
                {filters.date_from && <LemonTag>From: {filters.date_from}</LemonTag>}
                {filters.date_to && <LemonTag>To: {filters.date_to}</LemonTag>}
                {filters.order_by && <LemonTag>Order: {filters.order_by}</LemonTag>}
            </div>
            <LemonButton type="primary" size="small" onClick={handleApplyFilters}>
                Apply filters to Error Tracking
            </LemonButton>
        </MessageTemplate>
    )
}
