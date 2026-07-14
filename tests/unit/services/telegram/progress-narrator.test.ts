import { ProgressNarrator } from '../../../../src/services/telegram/progress-narrator';

describe('ProgressNarrator', () => {
  it('maps domains without exposing topology or scopes', () => {
    const narrator = new ProgressNarrator();
    narrator.start(0);
    expect(narrator.next(0)).toBe('Thinking…');
    narrator.record({ phase: 'routing', action: 'completed', domains: ['todoist', 'calendar'], intent: 'read' });
    expect(narrator.next(3_999)).toBeUndefined();
    expect(narrator.next(4_000)).toBe('Pulling up Todoist and Calendar…');
  });

  it('reports elapsed-time escalation bands', () => {
    const narrator = new ProgressNarrator();
    narrator.start(0);
    narrator.next(0); // renders 'Thinking…', lastRenderedAt=0
    expect(narrator.next(20_000)).toBeUndefined();
    // 45s band fires
    expect(narrator.next(45_000)).toBe('Taking a little longer than usual…');
    // No new band between 45s and 75s
    expect(narrator.next(60_000)).toBeUndefined();
    // 75s band fires
    expect(narrator.next(75_000)).toBe('Taking a little longer than usual — still working on it…');
    // 120s band fires
    expect(narrator.next(120_000)).toBe('Taking a little longer than usual — still working on it — this is taking longer than expected…');
  });
});
