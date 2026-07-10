import { ProgressNarrator } from '../../../../src/services/telegram/progress-narrator';

describe('ProgressNarrator', () => {
  it('maps domains without exposing topology or scopes', () => {
    const narrator = new ProgressNarrator();
    narrator.start(0);
    expect(narrator.next(0)).toBe('Reading your request…');
    narrator.record({ phase: 'routing', action: 'completed', domains: ['todoist', 'calendar'], intent: 'read' });
    expect(narrator.next(3_999)).toBeUndefined();
    expect(narrator.next(4_000)).toBe('Checking Todoist and Calendar…');
  });

  it('keeps an unchanged status alive and reports long-running work', () => {
    const narrator = new ProgressNarrator();
    narrator.start(0);
    narrator.next(0);
    expect(narrator.next(20_000)).toBe('Still working on this…');
    expect(narrator.next(45_000)).toBe('Still working — this is taking a little longer than usual…');
    expect(narrator.next(60_000)).toBe('Still working — this is taking a little longer than usual…');
    expect(narrator.next(115_000)).toBe('Still working — I’m continuing to check this.');
  });
});
