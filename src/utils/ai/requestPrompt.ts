export function formatRequestDateTime(date: Date = new Date()): string {
  return date.toISOString();
}

export function buildUserPromptWithRequestDateTime(
  userMessage: string,
  requestDate: Date = new Date(),
): string {
  return [
    'Request context:',
    `Current request date and time: ${formatRequestDateTime(requestDate)}`,
    '',
    'User request:',
    userMessage,
  ].join('\n');
}
