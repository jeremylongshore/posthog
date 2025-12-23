import { IconList } from '@posthog/icons'

import { addProductIntent } from 'lib/utils/product-intents'
import { useMaxTool } from 'scenes/max/useMaxTool'

import {
    ErrorTrackingExplainIssueToolContext,
    ErrorTrackingRelationalIssue,
    ProductIntentContext,
    ProductKey,
} from '~/queries/schema/schema-general'

export function useErrorTrackingExplainIssueMaxTool(
    issueId: ErrorTrackingRelationalIssue['id'],
    issueName: ErrorTrackingRelationalIssue['name']
): ReturnType<typeof useMaxTool> {
    const context: ErrorTrackingExplainIssueToolContext = {
        issue_id: issueId,
        issue_name: issueName ?? undefined,
    }

    const maxToolResult = useMaxTool({
        identifier: 'error_tracking_explain_issue',
        context,
        contextDescription: {
            text: 'Error tracking issue',
            icon: <IconList />,
        },
        active: !!issueId,
        initialMaxPrompt: `Explain this issue to me`,
        callback() {
            addProductIntent({
                product_type: ProductKey.ERROR_TRACKING,
                intent_context: ProductIntentContext.ERROR_TRACKING_ISSUE_EXPLAINED,
                metadata: { issue_id: issueId },
            })
        },
    })

    return maxToolResult
}
