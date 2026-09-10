const TELEGRAM_MAX_LENGTH = 4096;

export function splitMessage(text: string, maxLength: number = TELEGRAM_MAX_LENGTH): string[] {
  if (text.length <= maxLength) return [text];

  const chunks: string[] = [];
  let remaining = text;

  while (remaining.length > 0) {
    if (remaining.length <= maxLength) {
      chunks.push(remaining);
      break;
    }

    const splitIndex = findSplitPoint(remaining, maxLength);
    chunks.push(remaining.slice(0, splitIndex).trimEnd());
    remaining = remaining.slice(splitIndex).trimStart();
  }

  return chunks;
}

function findSplitPoint(text: string, maxLength: number): number {
  const searchWindow = text.slice(0, maxLength);

  const paragraphBreak = searchWindow.lastIndexOf('\n\n');
  if (paragraphBreak > maxLength * 0.3) return guardLink(text, paragraphBreak, maxLength);

  const lineBreak = searchWindow.lastIndexOf('\n');
  if (lineBreak > maxLength * 0.3) return guardLink(text, lineBreak, maxLength);

  const sentenceBreak = searchWindow.lastIndexOf('. ');
  if (sentenceBreak > maxLength * 0.3) return guardLink(text, sentenceBreak + 1, maxLength);

  const spaceBreak = searchWindow.lastIndexOf(' ');
  if (spaceBreak > maxLength * 0.3) return guardLink(text, spaceBreak, maxLength);

  return maxLength;
}

// If candidate falls inside a Markdown link, move to just before the link's '['.
// Falls through to hard cutoff if the link starts too early (< 30% of maxLength).
const LINK_PATTERN = /\[[^\]]+\]\((?:[^()\s]|\([^()]*\))*\)/g;

function guardLink(text: string, candidate: number, maxLength: number): number {
  LINK_PATTERN.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = LINK_PATTERN.exec(text)) !== null) {
    const start = m.index;
    const end = start + m[0].length;
    if (candidate > start && candidate < end) {
      return start > maxLength * 0.3 ? start : maxLength;
    }
  }
  return candidate;
}
