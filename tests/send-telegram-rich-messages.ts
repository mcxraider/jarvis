import 'dotenv/config';

const botToken = process.env.BOT_TOKEN;
const chatId = process.env.TEST_CHAT_ID;

if (!botToken || !chatId) {
  throw new Error('BOT_TOKEN and TEST_CHAT_ID must be set (for example, in .env)');
}

const chunks = [
  // [
  //   '**Bold text using double asterisks**',
  //   '',
  //   '__Bold text using double underscores__',
  //   '',
  //   '*Italic text using single asterisks*',
  //   '',
  //   '_Italic text using single underscores_',
  //   '',
  //   '~~This text is struck through~~',
  //   '',
  //   'Here is `inline fixed-width code`.',
  //   '',
  //   '==This text should be marked/highlighted==',
  //   '',
  //   '||This is spoiler text. Tap to reveal.||',
  // ],
  // [
  //   '[Open Telegram](https://t.me/)',
  //   '',
  //   '[Send an email](mailto:user@example.com)',
  //   '',
  //   '[Call this number](tel:+123456789)',
  //   '',
  //   '[Inline mention of a user](tg://user?id=123456789)',
  //   '',
  //   'Custom emoji: ![👍](tg://emoji?id=5368324170671202286)',
  //   '',
  //   'Dynamic time: ![22:45 tomorrow](tg://time?unix=1647531900&format=wDT)',
  //   '',
  //   'Inline math example: $x^2 + y^2$',
  //   '',
  //   'Auto-detected entities:',
  //   '\\#hashtag $USD +12345678901, card: 4242 4242 4242 4242, https://t.me t.me a@t.me /command @username',
  // ],
  // [
  //   '# Heading 1', '', '## Heading 2', '', '### Heading 3', '', '#### Heading 4', '',
  //   '##### Heading 5', '', '###### Heading 6', '',
  //   'This is a normal paragraph. It should appear as regular readable text.', '',
  //   'This is another paragraph separated by a blank line.',
  // ],
  // ['```python', 'def hello():', '    print("pre-formatted fixed-width code block written in Python")', '', 'hello()', '```'],
  // [
  //   'Above the divider.', '', '---', '', 'Below the divider.', '',
  //   '- Unordered list item using dash', '* Unordered list item using asterisk',
  //   '+ Unordered list item using plus', '', '1. Ordered list item one',
  //   '2. Ordered list item two', '3. Ordered list item three', '',
  //   '- [ ] Task list item not completed', '- [x] Completed task list item',
  // ],
  // [
  //   '> Block quotation started', '>', '> Block quotation continued on the next line',
  //   '> Block quotation continued on the same line', '>', '> The last line of the block quotation',
  // ],
  // ['| Header 1 | Header 2 |', '|:---------|:--------:|', '| left aligned | centered |', '| apple | banana |', '| long text here | 123 |'],
  // ['Text with a reference[^id1] and another reference[^id2].', '', '[^id1]: Definition of the first footnote.', '[^id2]: Definition of the second footnote.'],
  // ['$$E = mc^2$$', '', '```math', 'E = mc^2', '```'],
  [
    '## Example Nested Syntax Report for _Q1_', '',
    'Intro with <u>underlined text</u>, ==marked text==, and $x^2 + y^2$.', '',
    '**Bold _italic <u>underlined italic bold</u> italic_ bold**', '',
    '<u>In inline tags, nested **markdown** is parsed</u>', '',
    '> Quote with **bold text, ~~strikethrough, and <tg-spoiler>spoiler</tg-spoiler>~~**, plus [a link](https://t.me/).', '',
    '- List item with `code`, <sup>superscript</sup>, <sub>subscript</sub>, and a footnote[^note]',
    '- Another item with **bold <tg-spoiler><code>spoiler code</code></tg-spoiler>**',
    '- Another item with ~~strikethrough and <ins>inserted text</ins>~~', '',
    '| Metric | Value |', '|:-------|------:|', '| Speed  | **42** <sup>ms</sup> |',
    '| Status | <tg-spoiler>ready</tg-spoiler> |', '',
    '[^note]: Footnote with _italic text_ and <u>HTML underline</u>.',
  ],
  // [
  //   '# Details blocks can contain Markdown content:', '',
  //   '<details open><summary>Summary with **bold text**</summary>', '', '### Details heading', '',
  //   '- List item with _italic text_', '- List item with <tg-spoiler>spoiler</tg-spoiler>',
  //   '- List item with `inline code`', '', '</details>',
  // ],
  // --- Clarification messages with AI reasoning footnotes (matches renderClarificationBlock format) ---
  [
    '<details open><summary>Clarification needed [^reasoning]</summary>',
    '',
    'You mentioned "reschedule the meeting" — which meeting do you mean?',
    '',
    '1. **Team standup** · Today, 10:00',
    '2. **1:1 with Sarah** · Today, 14:00',
    '3. **Sprint review** · Tomorrow, 11:00',
    '',
    '[^reasoning]: _Reason_: Three meetings match your request, so I need to identify the correct one before updating your calendar.',
    '',
    '</details>',
  ],
  [
    '<details open><summary>Clarification needed</summary>',
    '',
    'What priority should I use for this Todoist task?',
    '',
    '- 🔴 **Priority 1** · Urgent',
    '- 🟡 **Priority 2** · High',
    '- 🔵 **Priority 3** · Normal',
    '- ⚪ **Priority 4** · Low',
    '',
    'Why I’m asking[^reasoning]',
    '',
    '[^reasoning]: _Reason_: Your previous personal tasks use different priority levels, so I don’t want to assume a default.',
    '',
    '</details>',
  ],
  // [
  //   '<details open><summary>Clarification[^reasoning]</summary>',
  //   '',
  //   'Should I block the full afternoon (13:00–17:00) or just create a 1-hour focus block?',
  //   '',
  //   '[^reasoning]: User asked to "block time for the proposal this afternoon." Their calendar shows 13:00–15:00 free and 15:30–17:00 free (with a 15-min break at 15:00). Creating one 4-hour block would require declining the 15:00 coffee chat with Mike. A shorter block avoids conflicts. Need user intent.',
  //   '',
  //   '</details>',
  // ],
  // [
  //   '<details open><summary>Clarification[^reasoning]</summary>',
  //   '',
  //   'You have two "Weekly Review" tasks — one in your **Work** project and one in **Personal**. Which one should I mark complete?',
  //   '',
  //   '[^reasoning]: Todoist search returned 2 exact matches for "weekly review": task #7234981 in Work (due today, p2) and task #7301822 in Personal (due tomorrow, p3). Both are recurring. Since today is Friday and the Work one is due today, it\'s probably that one — but marking the wrong recurring task complete would mess up their schedule.',
  //   '',
  //   '</details>',
  // ],
  // [
  //   '<details open><summary>Clarification[^reasoning]</summary>',
  //   '',
  //   'I found 6 tasks due this week. Want me to reschedule all of them to next Monday, or just the ones you haven\'t started yet?',
  //   '',
  //   '**Overdue tasks:**',
  //   '- ~~Write API docs~~ (was due Wed)',
  //   '- ~~Review PR #142~~ (was due Thu)',
  //   '- ~~Update env vars~~ (was due Thu)',
  //   '',
  //   '**Due today/tomorrow:**',
  //   '- Deploy staging build (today)',
  //   '- Send weekly update (today)',
  //   '- Prep Monday slides (tomorrow)',
  //   '',
  //   '[^reasoning]: User said "push everything to Monday, I\'m swamped." Literal interpretation = move all 6. But "deploy staging" and "send weekly update" are due today and may have downstream dependencies (the team expects the weekly update on Fridays). Moving those silently could cause issues. Safer to confirm scope.',
  //   '',
  //   '</details>',
  // ],
].map((lines) => lines.join('\n'));

async function sendChunk(markdown: string, index: number): Promise<void> {
  const response = await fetch(`https://api.telegram.org/bot${botToken}/sendRichMessage`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      chat_id: chatId,
      rich_message: { markdown },
    }),
  });

  const result = (await response.json()) as { ok?: boolean; description?: string };
  if (!response.ok || !result.ok) {
    throw new Error(`Chunk ${index} failed: ${result.description ?? response.statusText}`);
  }

  console.log(`Sent chunk ${index}`);
}

async function main(): Promise<void> {
  for (const [index, chunk] of chunks.entries()) {
    await sendChunk(chunk, index + 1);
  }
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
