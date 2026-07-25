I've added a debug log that will show exactly what Telegram delivers in reply_to_message — specifically:
- Whether .text exists and how long it is
- Whether .rich_message exists and its structure
- Whether the newer message.quote field exists (Telegram's partial-quote feature)

After deploying, reply to a bot message and check the logs for telegram.reply_to_message.debug. That will tell us whether:
1. Telegram provides rich_message at all on reply_to_message (it may not)
2. The .text field is genuinely truncated by Telegram before it even reaches us
3. The quote field on the message itself carries selected text

My suspicion: Telegram probably does NOT propagate rich_message back on reply_to_message — it just gives a plaintext summary in .text. If that's the case, the truncation is on Telegram's side and we'd need a different strategy (e.g., storing sent messages locally and looking them up by message_id). But the logs will confirm.


need to check this. check the recent log runs.