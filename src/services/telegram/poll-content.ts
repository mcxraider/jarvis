// src/services/telegram/poll-content.ts — Pure formatter: Telegram poll object → plain text.
// No side effects, no logger, no networking. Used by forward buffer and reply context.

export function formatPollAsText(poll: unknown): string | undefined {
  if (!poll || typeof poll !== 'object') return undefined;
  const p = poll as Record<string, unknown>;

  const question = typeof p.question === 'string' ? p.question.trim() : undefined;
  if (!question) return undefined;

  const options = Array.isArray(p.options) ? p.options : [];
  const optionLines = options
    .map((opt, i) => {
      if (!opt || typeof opt !== 'object') return undefined;
      const o = opt as Record<string, unknown>;
      const text = typeof o.text === 'string' ? o.text : undefined;
      if (!text) return undefined;
      const count =
        typeof o.voter_count === 'number' && Number.isFinite(o.voter_count)
          ? ` (${o.voter_count} vote${o.voter_count === 1 ? '' : 's'})`
          : '';
      return `${i + 1}. ${text}${count}`;
    })
    .filter((line): line is string => Boolean(line));

  if (optionLines.length === 0) return undefined;

  const lines: string[] = [
    'Telegram poll — received snapshot',
    `Question: ${question}`,
  ];

  if (typeof p.question_text === 'string' && p.question_text.trim()) {
    lines.push(`Description: ${p.question_text.trim()}`);
  }

  lines.push('Options:', ...optionLines);

  const meta: string[] = [];
  if (typeof p.type === 'string') meta.push(`Type: ${p.type}`);
  if (typeof p.allows_multiple_answers === 'boolean')
    meta.push(`Multiple answers allowed: ${p.allows_multiple_answers ? 'yes' : 'no'}`);
  if (typeof p.is_anonymous === 'boolean')
    meta.push(`Anonymous: ${p.is_anonymous ? 'yes' : 'no'}`);
  if (typeof p.is_closed === 'boolean')
    meta.push(`Closed: ${p.is_closed ? 'yes' : 'no'}`);
  if (typeof p.total_voter_count === 'number' && Number.isFinite(p.total_voter_count))
    meta.push(`Reported total voters: ${p.total_voter_count}`);

  // Quiz correct answer — index zero is valid, so check for number type, not truthiness.
  const correctId = p.correct_option_id;
  if (typeof correctId === 'number' && Number.isInteger(correctId) && correctId >= 0 && correctId < optionLines.length) {
    meta.push(`Correct answer: option ${correctId + 1}`);
  }
  if (typeof p.explanation === 'string' && p.explanation.trim()) {
    meta.push(`Explanation: ${p.explanation.trim()}`);
  }

  if (meta.length > 0) lines.push(...meta);

  lines.push('Your selected answer: not available in this snapshot');

  return lines.join('\n');
}
