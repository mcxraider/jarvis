import { ToolResult } from '../../../types/tool.types';

/**
 * Formats structured tool execution results without asking GPT to summarize facts.
 */
export class ToolResultFormatter {
  formatToolResults(toolResults: ToolResult[]): string {
    if (toolResults.length === 0) {
      return 'Could not complete the request.';
    }

    const successfulResults = toolResults.filter((result) => !result.error);
    const failedResults = toolResults.filter((result) => !!result.error);

    if (failedResults.length === 0) {
      if (successfulResults.length === 1) {
        return 'Done.';
      }
      return `Done — ${successfulResults.length} actions completed.`;
    }

    const failureSummary = this.formatFailures(failedResults);

    if (successfulResults.length === 0) {
      return `Failed:\n${failureSummary}`;
    }

    return `${successfulResults.length}/${toolResults.length} completed.\n\n<b>Failed:</b>\n${failureSummary}`;
  }

  private formatFailures(failedResults: ToolResult[]): string {
    return failedResults
      .map((result) => {
        const label = result.displayLabel || result.toolName || result.tool_call_id;
        return `• ${label}: ${result.error || 'Unknown error'}`;
      })
      .join('\n');
  }
}
