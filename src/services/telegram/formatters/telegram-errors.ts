export function isMessageNotModified(error: unknown): boolean {
  return /message is not modified/i.test(error instanceof Error ? error.message : String(error));
}

export function isMessageMissing(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /message to edit not found|message_id_invalid|message not found/i.test(message);
}
