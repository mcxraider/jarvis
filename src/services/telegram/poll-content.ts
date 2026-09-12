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
    `[poll] Question: ${question}`,
    'Options:',
    ...optionLines,
  ];

  // Collapsed metadata line: only actionable fields, pipe-separated.
  const metaParts: string[] = [];
  if (typeof poll.allows_multiple_answers === 'boolean')
    metaParts.push(`Multiple answers: ${poll.allows_multiple_answers ? 'yes' : 'no'}`);
  if (typeof poll.total_voter_count === 'number' && Number.isFinite(poll.total_voter_count))
    metaParts.push(`${poll.total_voter_count} voters`);
  if (poll.is_closed === true) metaParts.push('Closed');
  else if (metaParts.length > 0) metaParts.push('Open');

  const correctId = poll.correct_option_id;
  if (typeof correctId === 'number' && Number.isInteger(correctId) && correctId >= 0 && correctId < optionLines.length) {
    metaParts.push(`Correct answer: option ${correctId + 1}`);
  }
  if (typeof poll.explanation === 'string' && poll.explanation.trim()) {
    metaParts.push(`Explanation: ${poll.explanation.trim()}`);
  }

  if (metaParts.length > 0) lines.push(metaParts.join(' | '));

  return lines.join('\n');
}
