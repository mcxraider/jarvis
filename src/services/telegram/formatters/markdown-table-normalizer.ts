const FENCED_CODE_BLOCK = /(```[\s\S]*?```|~~~[\s\S]*?~~~)/g;
// Models commonly flatten Markdown newlines either as `||` or as `| |`
// (the closing pipe of one row followed by the opening pipe of the next).
const COMPACT_ROW_SEPARATOR = /\s*\|\s*\|\s*/;
const DELIMITER_CELL = /^:?-{3,}:?$/;

function isDelimiterRow(segment: string): boolean {
  const cells = segment
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());

  return cells.length >= 2 && cells.every((cell) => DELIMITER_CELL.test(cell));
}

function formatRow(cells: string[]): string {
  return `| ${cells.map((cell) => cell.trim()).join(' | ')} |`;
}

function splitCells(segment: string): string[] {
  return segment
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|');
}

function splitHeaderAndPrefix(
  segment: string,
  columnCount: number,
): { prefix: string; headerCells: string[] } | undefined {
  const parts = segment
    .trim()
    .replace(/^\|/, '')
    .split('|');

  if (parts.length < columnCount) return undefined;

  return {
    prefix: parts.slice(0, -columnCount).join('|').trim(),
    headerCells: parts.slice(-columnCount),
  };
}

/**
 * Repairs a compact Markdown table whose row breaks were accidentally emitted
 * as adjacent (optionally whitespace-separated) pipes instead of newlines:
 *
 *   Tuesday | Time | Event || --- | --- || 10:00 | Stand-up |
 *   Task | Due | | --- | --- | | Pay rent | Tomorrow |
 *
 * becomes:
 *
 *   Tuesday
 *   | Time | Event |
 *   | --- | --- |
 *   | 10:00 | Stand-up |
 *
 *   | Task | Due |
 *   | --- | --- |
 *   | Pay rent | Tomorrow |
 *
 * A delimiter row is required, which keeps ordinary prose containing `||`
 * unchanged. Fenced code blocks are deliberately excluded.
 */
export function normalizeMarkdownTables(markdown: string): string {
  return markdown
    .split(FENCED_CODE_BLOCK)
    .map((part, index) => {
      if (index % 2 === 1) return part;

      return part
        .split('\n')
        .map((line) => {
          const segments = line.split(COMPACT_ROW_SEPARATOR);
          const delimiterIndex = segments.findIndex(isDelimiterRow);

          if (delimiterIndex < 1) return line;

          const columnCount = splitCells(segments[delimiterIndex]).length;
          const header = splitHeaderAndPrefix(segments[delimiterIndex - 1], columnCount);

          if (!header) return line;

          const rows = [
            formatRow(header.headerCells),
            formatRow(splitCells(segments[delimiterIndex])),
          ];

          for (const segment of segments.slice(delimiterIndex + 1)) {
            const cells = splitCells(segment);
            if (cells.length !== columnCount) return line;
            rows.push(formatRow(cells));
          }

          return header.prefix ? `${header.prefix}\n${rows.join('\n')}` : rows.join('\n');
        })
        .join('\n');
    })
    .join('');
}
