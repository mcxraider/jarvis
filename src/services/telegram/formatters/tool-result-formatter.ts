// src/services/telegram/formatters/tool-result-formatter.ts — Formats LangGraph tool
// execution results into user-facing summary text. Shows a success count for clean
// runs, or a bulleted failure list with labels when some tools error out.

export interface ToolResult {
  tool_call_id: string;
  toolName?: string;
  displayLabel?: string;
  content: any;
  error?: string;
}

export class ToolResultFormatter {
  // Produces a one-line summary for all-success cases, or a detailed breakdown
  // listing which tools failed and why.
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

    return `${successfulResults.length}/${toolResults.length} completed.\n\n**Failed:**\n${failureSummary}`;
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
