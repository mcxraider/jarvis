import { normalizeMarkdownTables } from '../../../../../src/services/telegram/formatters/markdown-table-normalizer';

describe('normalizeMarkdownTables', () => {
  it('repairs a compact table while preserving its leading label', () => {
    const input =
      '**Tuesday, Jun 30** | Time | Event ||---|---|| 🕚 11:00 – 12:00 | DS / Fraud Priorities || 🕜 13:30 – 14:00 | Jerry <> Daniel weekly sync |';

    expect(normalizeMarkdownTables(input)).toBe(
      [
        '**Tuesday, Jun 30**',
        '| Time | Event |',
        '| --- | --- |',
        '| 🕚 11:00 – 12:00 | DS / Fraud Priorities |',
        '| 🕜 13:30 – 14:00 | Jerry <> Daniel weekly sync |',
      ].join('\n'),
    );
  });

  it('repairs whitespace-separated row pipes from a flattened Todoist response', () => {
    const input =
      "📅 Birthdays (recurring) — labelled 🎂 birthdays | Task | Due | Priority | |------|-----|:--------:| | Jarod's bday | Every 17 Jan | 🔴 P1 | | Davidoff birthday | Every 23 Jan | 🔴 P1 |";

    expect(normalizeMarkdownTables(input)).toBe(
      [
        '📅 Birthdays (recurring) — labelled 🎂 birthdays',
        '| Task | Due | Priority |',
        '| ------ | ----- | :--------: |',
        "| Jarod's bday | Every 17 Jan | 🔴 P1 |",
        '| Davidoff birthday | Every 23 Jan | 🔴 P1 |',
      ].join('\n'),
    );
  });

  it('repairs compact tables without a leading label', () => {
    const input = 'Task | Due | | --- | --- | | Pay rent | Tomorrow | | Buy milk | Friday |';

    expect(normalizeMarkdownTables(input)).toBe(
      [
        '| Task | Due |',
        '| --- | --- |',
        '| Pay rent | Tomorrow |',
        '| Buy milk | Friday |',
      ].join('\n'),
    );
  });

  it('repairs compact tables generically across column names and counts', () => {
    const input =
      'Inventory | SKU | Qty | Owner | Status | |:---|---:|:---:|---| | AX-1 | 12 | Ops | Ready | | BX-2 | 0 | Sales | Backordered |';

    expect(normalizeMarkdownTables(input)).toBe(
      [
        'Inventory',
        '| SKU | Qty | Owner | Status |',
        '| :--- | ---: | :---: | --- |',
        '| AX-1 | 12 | Ops | Ready |',
        '| BX-2 | 0 | Sales | Backordered |',
      ].join('\n'),
    );
  });

  it('repairs flattened one-row Todoist add-confirmation tables', () => {
    const input = [
      "All set! Here's what I've added to your Todoist:",
      '',
      '📅 Friday, 10 Jul | Task | Time | |------|------| | Worship practice | 7:30 pm |',
      '',
      '📅 Sunday, 12 Jul | Task | Time | Duration | |------|------|----------| | Serving worship | 8:30 am – 12:00 pm | 3h 30m | | Lunch with feebee | 12:00 pm | — |',
      '',
      'No timing clashes between any of these.',
    ].join('\n');

    expect(normalizeMarkdownTables(input)).toBe(
      [
        "All set! Here's what I've added to your Todoist:",
        '',
        '📅 Friday, 10 Jul',
        '| Task | Time |',
        '| ------ | ------ |',
        '| Worship practice | 7:30 pm |',
        '',
        '📅 Sunday, 12 Jul',
        '| Task | Time | Duration |',
        '| ------ | ------ | ---------- |',
        '| Serving worship | 8:30 am – 12:00 pm | 3h 30m |',
        '| Lunch with feebee | 12:00 pm | — |',
        '',
        'No timing clashes between any of these.',
      ].join('\n'),
    );
  });

  it('leaves valid multiline tables unchanged', () => {
    const input = '| Event | Date |\n| --- | --- |\n| Conference | Monday |';
    expect(normalizeMarkdownTables(input)).toBe(input);
  });

  it('leaves non-table double pipes and fenced code unchanged', () => {
    const input = [
      'Use left || right in prose.',
      '```md',
      'Label | A | B || --- | --- || one | two |',
      '```',
    ].join('\n');

    expect(normalizeMarkdownTables(input)).toBe(input);
  });

  it('does not partially rewrite malformed rows with the wrong column count', () => {
    const input = 'Label | A | B || --- | --- || only one cell |';
    expect(normalizeMarkdownTables(input)).toBe(input);
  });
});
