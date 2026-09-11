// src/services/telegram/poll-content.ts — Pure formatter: Telegram poll object → plain text.
// No side effects, no logger, no networking. Used by forward buffer and reply context.

import { Poll } from 'telegraf/typings/core/types/typegram';

export function formatPollAsText(poll: Poll): string | undefined {
  if (!poll || typeof poll !== 'object') return undefined;

  const question = poll.question?.trim();
  if (!question) return undefined;

  const options = Array.isArray(poll.options) ? poll.options : [];
  const optionLines = options
    .map((opt, i) => {
      if (!opt || typeof opt !== 'object') return undefined;
      const text = typeof opt.text === 'string' ? opt.text : undefined;
      if (!text) return undefined;
      const count =
        typeof opt.voter_count === 'number' && Number.isFinite(opt.voter_count)
          ? ` (${opt.voter_count} vote${opt.voter_count === 1 ? '' : 's'})`
          : '';
      return `${i + 1}. ${text}${count}`;
    })
    .filter((line): line is string => Boolean(line));

  if (optionLines.length === 0) return undefined;

  const lines: string[] = [
    'Telegram poll — received snapshot',
    `Question: ${question}`,
  ];

  lines.push('Options:', ...optionLines);

  const meta: string[] = [];
  if (typeof poll.type === 'string') meta.push(`Type: ${poll.type}`);
  if (typeof poll.allows_multiple_answers === 'boolean')
    meta.push(`Multiple answers allowed: ${poll.allows_multiple_answers ? 'yes' : 'no'}`);
  if (typeof poll.is_anonymous === 'boolean')
    meta.push(`Anonymous: ${poll.is_anonymous ? 'yes' : 'no'}`);
  if (typeof poll.is_closed === 'boolean')
    meta.push(`Closed: ${poll.is_closed ? 'yes' : 'no'}`);
  if (typeof poll.total_voter_count === 'number' && Number.isFinite(poll.total_voter_count))
    meta.push(`Reported total voters: ${poll.total_voter_count}`);

  const correctId = poll.correct_option_id;
  if (typeof correctId === 'number' && Number.isInteger(correctId) && correctId >= 0 && correctId < optionLines.length) {
    meta.push(`Correct answer: option ${correctId + 1}`);
  }
  if (typeof poll.explanation === 'string' && poll.explanation.trim()) {
    meta.push(`Explanation: ${poll.explanation.trim()}`);
  }

  if (meta.length > 0) lines.push(...meta);

  lines.push('Your selected answer: not available in this snapshot');

  return lines.join('\n');
}
