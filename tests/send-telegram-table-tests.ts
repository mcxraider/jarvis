import 'dotenv/config';

const botToken = process.env.BOT_TOKEN;
const chatId = process.env.TEST_CHAT_ID;

if (!botToken || !chatId) {
  throw new Error('BOT_TOKEN and TEST_CHAT_ID must be set (for example, in .env)');
}

interface TestCase {
  name: string;
  markdown: string;
}

const cases: TestCase[] = [
  // === BASIC TABLES ===
  {
    name: '1. Simple 2-col table, no alignment',
    markdown: [
      '| Day | Item |',
      '| --- | --- |',
      '| Monday | Dentist |',
      '| Tuesday | Gym |',
    ].join('\n'),
  },
  {
    name: '2. Simple 3-col table with alignment markers',
    markdown: [
      '| Time | Task | Priority |',
      '|:-----|:----:|-------:|',
      '| 9:00 AM | Stand-up | High |',
      '| 2:00 PM | Code review | Medium |',
    ].join('\n'),
  },
  {
    name: '3. Table with bold cells',
    markdown: [
      '| Day | Item | Time |',
      '|------|------|------|',
      '| **Tue Jul 28** | Return SEED laptop | 9am |',
      '| **Wed Jul 29** | Head to Phoebe\'s house | 9am |',
    ].join('\n'),
  },
  {
    name: '4. Table with emoji in cells',
    markdown: [
      '| Time | Task |',
      '|------|------|',
      '| 🕘 9:00 AM | Return SEED laptop ← new 🔥 P1 |',
      '| 🌙 7:00 PM | Meet Mexico Gang |',
      '| All day | round 2 coursereg, Office |',
    ].join('\n'),
  },
  {
    name: '5. Table with many rows (15 events)',
    markdown: [
      '| Day | Item | Time |',
      '|------|------|------|',
      '| Tue Jul 28 | Return SEED laptop | 9am |',
      '| | Office (recurring) | all day |',
      '| | Meet Mexico Gang | 7pm |',
      '| Wed Jul 29 | Head to Phoebe\'s house | 9am |',
      '| | Watch Odyssey with Phoebe | 7:30pm |',
      '| Thu Jul 30 | Office (one-off) | 10am |',
      '| | MWTS - Reading Biblical Narrative | 8pm |',
      '| | dinner with chongsun | 7pm |',
      '| Fri Jul 31 | Work from home with Phoebe | 9am |',
      '| | Shane farewell | 7pm |',
      '| Sat Aug 1 | Morning run | 9am |',
      '| | cafe work | 11am |',
      '| | Reservation at Blu Kouzina | 7:45pm |',
      '| | AG (recurring) | 4pm |',
      '| Sun Aug 2 | attend 430 | 4pm |',
    ].join('\n'),
  },

  // === TABLES WITH SURROUNDING TEXT ===
  {
    name: '6. Text before and after table',
    markdown: [
      'All clear — no clashes tomorrow morning. Here\'s what\'s on deck for **Tue 28 Jul**:',
      '',
      '| Time | Task |',
      '|------|------|',
      '| 9:00 AM | Return SEED laptop |',
      '| 7:00 PM | Meet Mexico Gang |',
      '',
      'Done — you\'re set for tomorrow morning 👉',
    ].join('\n'),
  },
  {
    name: '7. Multiple tables with text between',
    markdown: [
      'All 19 items labelled. Here\'s the week at a glance:',
      '📅 **Events (15)**',
      '',
      '| Day | Item | Time |',
      '|------|------|------|',
      '| **Tue Jul 28** | Return SEED laptop | 9am |',
      '| | Office (recurring) | all day |',
      '| | Meet Mexico Gang | 7pm |',
      '| **Wed Jul 29** | Head to Phoebe\'s house | 9am |',
      '',
      '✅ **Tasks (4)**',
      '',
      '| Day | Item |',
      '|------|------|',
      '| **Today Jul 27** | Set up Apple Pay connection to Notion |',
      '| | Get back to flower shop on which flowers to buy (due 10pm) |',
      '| **Tue Jul 28** | round 2 coursereg |',
      '| **Thu Jul 30** | Let prof know about IT1244 TA |',
    ].join('\n'),
  },
  {
    name: '8. Heading + table + footer',
    markdown: [
      '## Tomorrow\'s Schedule',
      '',
      '| Time | Event | Notes |',
      '|------|-------|-------|',
      '| 9:00 | Standup | Remote |',
      '| 14:00 | 1:1 with Sarah | Office |',
      '',
      '_Last synced 2 minutes ago._',
    ].join('\n'),
  },

  // === EDGE CASES: CELL CONTENT ===
  {
    name: '9. Table with inline code in cells',
    markdown: [
      '| Command | Description |',
      '|---------|-------------|',
      '| `npm run dev` | Start dev server |',
      '| `npm test` | Run tests |',
    ].join('\n'),
  },
  {
    name: '10. Table with links in cells',
    markdown: [
      '| Resource | Link |',
      '|----------|------|',
      '| Docs | [Documentation](https://example.com/docs) |',
      '| API | [API Reference](https://example.com/api) |',
    ].join('\n'),
  },
  {
    name: '11. Table with special characters (pipes escaped)',
    markdown: [
      '| Expression | Result |',
      '|------------|--------|',
      '| 5 \\| 3 | bitwise or |',
      '| a → b | implication |',
      '| x & y | logical and |',
    ].join('\n'),
  },
  {
    name: '12. Table with empty cells',
    markdown: [
      '| Day | Morning | Afternoon | Evening |',
      '|-----|---------|-----------|---------|',
      '| Mon | Gym | | Dinner |',
      '| Tue | | Meeting | |',
      '| Wed | Run | | Movie |',
    ].join('\n'),
  },
  {
    name: '13. Table with very long cell content',
    markdown: [
      '| Task | Details |',
      '|------|---------|',
      '| Get back to flower shop on which flowers to buy for the anniversary dinner | due 10pm tonight, call them before closing |',
      '| Short | ok |',
    ].join('\n'),
  },

  // === EDGE CASES: STRUCTURE ===
  {
    name: '14. Single-column table',
    markdown: [
      '| Items |',
      '|-------|',
      '| Apple |',
      '| Banana |',
      '| Cherry |',
    ].join('\n'),
  },
  {
    name: '15. Wide table (6 columns)',
    markdown: [
      '| Day | Time | Event | Location | Priority | Notes |',
      '|-----|------|-------|----------|----------|-------|',
      '| Mon | 9am | Standup | Zoom | High | Weekly |',
      '| Tue | 2pm | Review | Office | Med | Sprint |',
    ].join('\n'),
  },
  {
    name: '16. Table without leading/trailing pipes',
    markdown: [
      'Day | Item | Time',
      '--- | --- | ---',
      'Mon | Gym | 7am',
      'Tue | Work | 9am',
    ].join('\n'),
  },
  {
    name: '17. Table with only header and separator (no data rows)',
    markdown: [
      '| Column A | Column B |',
      '|----------|----------|',
    ].join('\n'),
  },
  {
    name: '18. Table with single data row',
    markdown: [
      '| Status | Count |',
      '|--------|-------|',
      '| Active | 3 |',
    ].join('\n'),
  },

  // === EDGE CASES: FORMATTING MIX ===
  {
    name: '19. Table with bold+italic+strikethrough in cells',
    markdown: [
      '| Status | Task |',
      '|--------|------|',
      '| ~~Done~~ | ~~Buy groceries~~ |',
      '| **Active** | *Write report* |',
      '| _Pending_ | **_Review PR_** |',
    ].join('\n'),
  },
  {
    name: '20. Table inside a blockquote',
    markdown: [
      '> Summary of today:',
      '>',
      '> | Time | Event |',
      '> |------|-------|',
      '> | 9am | Meeting |',
      '> | 2pm | Call |',
    ].join('\n'),
  },
  {
    name: '21. Table after a code block',
    markdown: [
      '```',
      'some code here',
      '```',
      '',
      '| Result | Value |',
      '|--------|-------|',
      '| Pass | 42 |',
    ].join('\n'),
  },
  {
    name: '22. Table after a list',
    markdown: [
      'Tasks completed:',
      '- Bought groceries',
      '- Called dentist',
      '',
      'Upcoming:',
      '',
      '| Day | Task |',
      '|-----|------|',
      '| Tomorrow | Submit report |',
      '| Friday | Team lunch |',
    ].join('\n'),
  },

  // === EDGE CASES: PROBLEMATIC PATTERNS ===
  {
    name: '23. Table with separator having different dash lengths',
    markdown: [
      '| A | B |',
      '|--|------------|',
      '| 1 | Two |',
    ].join('\n'),
  },
  {
    name: '24. Table with extra spaces in cells',
    markdown: [
      '|  Day  |  Item  |  Time  |',
      '|-------|--------|--------|',
      '|  Mon  |  Gym   |  7am   |',
      '|  Tue  |  Work  |  9am   |',
    ].join('\n'),
  },
  {
    name: '25. Table with unicode/CJK characters',
    markdown: [
      '| 日期 | 事件 | 時間 |',
      '|------|------|------|',
      '| 週一 | 會議 | 上午9點 |',
      '| 週二 | 午餐 | 中午12點 |',
    ].join('\n'),
  },
  {
    name: '26. Compact single-line table (model flattened output)',
    markdown: 'Day | Item | Time | --- | --- | --- | Mon | Gym | 7am | Tue | Work | 9am |',
  },
  {
    name: '27. Table with double-pipe row separators (model artifact)',
    markdown: 'Day | Item || --- | --- || Mon | Gym || Tue | Work |',
  },
  {
    name: '28. Table immediately after text (no blank line)',
    markdown: [
      'Here are your tasks:',
      '| Task | Due |',
      '|------|-----|',
      '| Buy milk | Today |',
      '| Call mom | Tomorrow |',
    ].join('\n'),
  },
  {
    name: '29. Table with trailing text on same line after last row',
    markdown: [
      '| A | B |',
      '|---|---|',
      '| 1 | 2 |',
      'That\'s all!',
    ].join('\n'),
  },
  {
    name: '30. Nested bold in header cells',
    markdown: [
      '| **Day** | **Item** | **Time** |',
      '|---------|----------|----------|',
      '| Mon | Standup | 9am |',
      '| Tue | Review | 2pm |',
    ].join('\n'),
  },

  // === REALISTIC JARVIS OUTPUT PATTERNS ===
  {
    name: '31. Daily brief with emoji headers + two tables (real pattern)',
    markdown: [
      'All 19 items labelled. Here\'s the week at a glance:',
      '📅 **Events (15)**',
      '',
      '| Day | Item | Time |',
      '|------|------|------|',
      '| **Tue Jul 28** | Return SEED laptop | 9am |',
      '| | Office (recurring) | all day |',
      '| | Meet Mexico Gang | 7pm |',
      '| **Wed Jul 29** | Head to Phoebe\'s house | 9am |',
      '| | Watch Odyssey with Phoebe | 7:30pm |',
      '| **Thu Jul 30** | Office (one-off) | 10am |',
      '| | MWTS - Reading Biblical Narrative | 8pm |',
      '| | dinner with chongsun | 7pm |',
      '| **Fri Jul 31** | Work from home with Phoebe | 9am |',
      '| | Shane farewell | 7pm |',
      '| **Sat Aug 1** | Morning run | 9am |',
      '| | cafe work | 11am |',
      '| | Reservation at Blu Kouzina | 7:45pm |',
      '| | AG (recurring) | 4pm |',
      '| **Sun Aug 2** | attend 430 | 4pm |',
      '',
      '✅ **Tasks (4)**',
      '',
      '| Day | Item |',
      '|------|------|',
      '| **Today Jul 27** | Set up Apple Pay connection to Notion |',
      '| | Get back to flower shop on which flowers to buy (due 10pm) |',
      '| **Tue Jul 28** | round 2 coursereg |',
      '| **Thu Jul 30** | Let prof know about IT1244 TA |',
    ].join('\n'),
  },
  {
    name: '32. Tomorrow schedule with emoji time markers (real pattern)',
    markdown: [
      'All clear — no clashes tomorrow morning. Here\'s what\'s on deck for **Tue 28 Jul**:',
      '',
      '| Time | Task |',
      '|------|------|',
      '| 🕘 9:00 AM | **Return SEED laptop** ← new 🔥 P1 |',
      '| 🌙 7:00 PM | Meet Mexico Gang |',
      '| All day | round 2 coursereg, Office |',
      '',
      'Done — you\'re set for tomorrow morning 👉',
    ].join('\n'),
  },
  {
    name: '33. Task completion summary table',
    markdown: [
      'Done ✓ Completed 3 tasks:',
      '',
      '| Task | Was due |',
      '|------|---------|',
      '| ~~Buy groceries~~ | Today |',
      '| ~~Submit timesheet~~ | Yesterday (overdue) |',
      '| ~~Reply to Sarah~~ | Today 5pm |',
    ].join('\n'),
  },
  {
    name: '34. Free/busy response table',
    markdown: [
      'Here\'s your availability for **Thu Jul 30**:',
      '',
      '| Slot | Status |',
      '|------|--------|',
      '| 9:00–10:00 | 🔴 Office (one-off) |',
      '| 10:00–12:00 | 🟢 Free |',
      '| 12:00–13:00 | 🟢 Free |',
      '| 13:00–14:00 | 🟢 Free |',
      '| 14:00–15:00 | 🟢 Free |',
      '| 15:00–17:00 | 🟢 Free |',
      '| 19:00–20:00 | 🔴 dinner with chongsun |',
      '| 20:00–21:30 | 🔴 MWTS - Reading Biblical Narrative |',
    ].join('\n'),
  },
  {
    name: '35. Minimal table — no alignment, minimal dashes',
    markdown: [
      '|A|B|',
      '|-|-|',
      '|1|2|',
    ].join('\n'),
  },
  {
    name: '36. Table with HTML tags in cells (tg-spoiler)',
    markdown: [
      '| Metric | Value |',
      '|--------|-------|',
      '| Speed | **42** ms |',
      '| Secret | <tg-spoiler>classified</tg-spoiler> |',
    ].join('\n'),
  },
  {
    name: '37. Table with superscript/subscript in cells',
    markdown: [
      '| Formula | Result |',
      '|---------|--------|',
      '| x<sup>2</sup> | 4 |',
      '| H<sub>2</sub>O | water |',
    ].join('\n'),
  },
  {
    name: '38. Large table (20+ rows) to test size limits',
    markdown: [
      '| # | Task | Status |',
      '|---|------|--------|',
      ...Array.from({ length: 25 }, (_, i) => `| ${i + 1} | Task number ${i + 1} with some description | ${i % 3 === 0 ? '✅' : i % 3 === 1 ? '⏳' : '❌'} |`),
    ].join('\n'),
  },
  {
    name: '39. Table with em-dash and special punctuation',
    markdown: [
      '| Event | Time | Notes |',
      '|-------|------|-------|',
      '| Coffee — with Mike | 10am | "important" |',
      '| Lunch (team) | 12:30pm | @ canteen |',
      '| 1:1 w/ Sarah… | 3pm | re: Q3 goals |',
    ].join('\n'),
  },
  {
    name: '40. Table preceded by horizontal rule',
    markdown: [
      'Section 1 complete.',
      '',
      '---',
      '',
      '| Next | Action |',
      '|------|--------|',
      '| Deploy | staging |',
      '| Notify | team |',
    ].join('\n'),
  },
];

async function sendTest(tc: TestCase): Promise<{ name: string; ok: boolean; error?: string }> {
  try {
    const response = await fetch(`https://api.telegram.org/bot${botToken}/sendRichMessage`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        chat_id: chatId,
        rich_message: { markdown: tc.markdown },
      }),
    });

    const result = (await response.json()) as { ok?: boolean; description?: string };
    if (!response.ok || !result.ok) {
      return { name: tc.name, ok: false, error: result.description ?? response.statusText };
    }
    return { name: tc.name, ok: true };
  } catch (err) {
    return { name: tc.name, ok: false, error: (err as Error).message };
  }
}

async function main(): Promise<void> {
  console.log(`Running ${cases.length} table format tests against sendRichMessage...\n`);

  const results: { name: string; ok: boolean; error?: string }[] = [];

  for (const tc of cases) {
    const result = await sendTest(tc);
    results.push(result);
    const icon = result.ok ? '✅' : '❌';
    console.log(`${icon} ${result.name}${result.error ? ` — ${result.error}` : ''}`);
    // Small delay to avoid rate limiting
    await new Promise((r) => setTimeout(r, 300));
  }

  console.log('\n' + '='.repeat(60));
  console.log('SUMMARY');
  console.log('='.repeat(60));

  const passed = results.filter((r) => r.ok);
  const failed = results.filter((r) => !r.ok);

  console.log(`\nPassed: ${passed.length}/${results.length}`);
  console.log(`Failed: ${failed.length}/${results.length}`);

  if (failed.length > 0) {
    console.log('\nFailed cases:');
    for (const f of failed) {
      console.log(`  ❌ ${f.name}`);
      console.log(`     Error: ${f.error}`);
    }
  }

  if (passed.length > 0) {
    console.log('\nPassed cases:');
    for (const p of passed) {
      console.log(`  ✅ ${p.name}`);
    }
  }

  // Group errors by type
  const errorGroups = new Map<string, string[]>();
  for (const f of failed) {
    const key = f.error ?? 'unknown';
    if (!errorGroups.has(key)) errorGroups.set(key, []);
    errorGroups.get(key)!.push(f.name);
  }

  if (errorGroups.size > 0) {
    console.log('\nErrors grouped:');
    for (const [error, names] of errorGroups) {
      console.log(`  "${error}" (${names.length} cases):`);
      for (const n of names) console.log(`    - ${n}`);
    }
  }
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
