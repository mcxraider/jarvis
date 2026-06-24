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
  if (paragraphBreak > maxLength * 0.3) return paragraphBreak;

  const lineBreak = searchWindow.lastIndexOf('\n');
  if (lineBreak > maxLength * 0.3) return lineBreak;

  const sentenceBreak = searchWindow.lastIndexOf('. ');
  if (sentenceBreak > maxLength * 0.3) return sentenceBreak + 1;

  const spaceBreak = searchWindow.lastIndexOf(' ');
  if (spaceBreak > maxLength * 0.3) return spaceBreak;

  return maxLength;
}
