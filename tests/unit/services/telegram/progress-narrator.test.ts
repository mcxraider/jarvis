import {
  PROGRESS_RICH_REFRESH_MS,
  ProgressNarrator,
  ProgressRender,
} from '../../../../src/services/telegram/progress-narrator';

function deliver(narrator: ProgressNarrator, now: number, keepaliveMs?: number): ProgressRender {
  const render = narrator.nextDesired(now, keepaliveMs);
  expect(render).toBeDefined();
  narrator.markDelivered(render!, now);
  return render!;
}

describe('ProgressNarrator', () => {
  it('maps domains and respects the four-second phase dwell', () => {
    const narrator = new ProgressNarrator();
    narrator.start(0);
    expect(deliver(narrator, 0).label).toBe('Thinking…');

    narrator.record({
      phase: 'routing', action: 'completed', domains: ['todoist', 'calendar'], intent: 'read',
    }, 1);
    expect(narrator.nextDesired(3_999)).toBeUndefined();
    expect(narrator.nextDesired(4_000)).toEqual(expect.objectContaining({
      label: 'Pulling up Todoist and Calendar…',
      reason: 'phase',
      phase: 'routing',
      sequence: 1,
    }));
  });

  it('renders non-cumulative elapsed reassurance at 45, 75, and 120 seconds', () => {
    const narrator = new ProgressNarrator();
    narrator.start(0);
    deliver(narrator, 0);

    expect(narrator.nextDesired(44_999)).toBeUndefined();
    expect(deliver(narrator, 45_000).label).toBe('Thinking — taking a little longer…');
    expect(deliver(narrator, 75_000).label).toBe('Thinking — still working…');
    expect(deliver(narrator, 120_000).label).toBe('Thinking — taking longer than expected…');
  });

  it('keeps the latest graph phase when elapsed bands change', () => {
    const narrator = new ProgressNarrator();
    narrator.start(0);
    deliver(narrator, 0);
    deliver(narrator, 45_000);

    narrator.record({
      phase: 'lookup', action: 'started', domains: ['calendar'], intent: 'read',
    }, 2);
    expect(deliver(narrator, 49_000).label)
      .toBe('Pulling up Calendar — taking a little longer…');
    expect(deliver(narrator, 75_000).label)
      .toBe('Pulling up Calendar — still working…');

    narrator.record({ phase: 'review', action: 'completed', intent: 'read' }, 3);
    expect(deliver(narrator, 79_000).label)
      .toBe('Reviewing what I found — still working…');
    expect(deliver(narrator, 120_000).label)
      .toBe('Reviewing what I found — taking longer than expected…');
  });

  it('coalesces bursts to the latest sequence and ignores stale events', () => {
    const narrator = new ProgressNarrator();
    narrator.start(0);
    deliver(narrator, 0);

    narrator.record({ phase: 'routing', action: 'completed', intent: 'read' }, 1);
    narrator.record({
      phase: 'lookup', action: 'started', domains: ['calendar'], intent: 'read',
    }, 2);
    narrator.record({ phase: 'routing', action: 'completed', intent: 'read' }, 1);

    expect(narrator.nextDesired(3_999)).toBeUndefined();
    expect(deliver(narrator, 4_000).label).toBe('Pulling up Calendar…');
  });

  it('keeps an attempted render pending until delivery is acknowledged', () => {
    const narrator = new ProgressNarrator();
    narrator.start(0);
    deliver(narrator, 0, PROGRESS_RICH_REFRESH_MS);
    narrator.record({ phase: 'review', action: 'completed', intent: 'read' }, 1);

    const attempted = narrator.nextDesired(4_000, PROGRESS_RICH_REFRESH_MS);
    expect(attempted?.label).toBe('Reviewing what I found…');
    expect(narrator.nextDesired(4_001, PROGRESS_RICH_REFRESH_MS))
      .toEqual(expect.objectContaining({ label: 'Reviewing what I found…' }));
  });
});
