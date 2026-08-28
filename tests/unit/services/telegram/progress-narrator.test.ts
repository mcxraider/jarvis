import {
  PROGRESS_RICH_REFRESH_MS,
  ProgressNarrator,
  ProgressRender,
  seedLabelForInputKind,
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
    narrator.start(undefined, 0);
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
    narrator.start(undefined, 0);
    deliver(narrator, 0);

    expect(narrator.nextDesired(44_999)).toBeUndefined();
    expect(deliver(narrator, 45_000).label).toBe('Thinking — taking a little longer…');
    expect(deliver(narrator, 75_000).label).toBe('Thinking — still working…');
    expect(deliver(narrator, 120_000).label).toBe('Thinking — taking longer than expected…');
  });

  it('keeps the latest graph phase when elapsed bands change', () => {
    const narrator = new ProgressNarrator();
    narrator.start(undefined, 0);
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
      .toBe('Reviewing — still working…');
    expect(deliver(narrator, 120_000).label)
      .toBe('Reviewing — taking longer than expected…');
  });

  it('coalesces bursts to the latest sequence and ignores stale events', () => {
    const narrator = new ProgressNarrator();
    narrator.start(undefined, 0);
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
    narrator.start(undefined, 0);
    deliver(narrator, 0, PROGRESS_RICH_REFRESH_MS);
    narrator.record({ phase: 'review', action: 'completed', intent: 'read' }, 1);

    const attempted = narrator.nextDesired(4_000, PROGRESS_RICH_REFRESH_MS);
    expect(attempted?.label).toBe('Reviewing…');
    expect(narrator.nextDesired(4_001, PROGRESS_RICH_REFRESH_MS))
      .toEqual(expect.objectContaining({ label: 'Reviewing…' }));
  });

  it('seeds the initial label from the input kind before any graph phase arrives', () => {
    const audioNarrator = new ProgressNarrator();
    audioNarrator.start(seedLabelForInputKind('audio'), 0);
    expect(deliver(audioNarrator, 0).label).toBe('Listening…');

    const forwardedNarrator = new ProgressNarrator();
    forwardedNarrator.start(seedLabelForInputKind('forwarded'), 0);
    expect(deliver(forwardedNarrator, 0).label).toBe('Reviewing forwarded messages…');
  });

  it('does not let a generic request-phase event override an input-specific seed label', () => {
    const narrator = new ProgressNarrator();
    narrator.start('Listening…', 0);
    narrator.record({ phase: 'request', action: 'started', intent: 'read' }, 1);
    expect(deliver(narrator, 0).label).toBe('Listening…');
  });

  it('still applies a later semantic graph phase over an input-specific seed label', () => {
    const narrator = new ProgressNarrator();
    narrator.start(seedLabelForInputKind('audio'), 0);
    deliver(narrator, 0);

    narrator.record({ phase: 'review', action: 'completed', intent: 'read' }, 1);
    expect(deliver(narrator, 4_000).label).toBe('Reviewing…');
  });

  it('advanceToThinking transitions from an input-specific seed to the generic Thinking label', () => {
    const narrator = new ProgressNarrator();
    narrator.start(seedLabelForInputKind('audio'), 0);
    deliver(narrator, 0);

    narrator.advanceToThinking(4_000);
    expect(deliver(narrator, 4_000).label).toBe('Thinking…');
  });

  it('advanceToThinking preserves startedAt so the elapsed-band suffix still applies', () => {
    const narrator = new ProgressNarrator();
    narrator.start(seedLabelForInputKind('audio'), 0);
    deliver(narrator, 0);
    deliver(narrator, 45_000);

    narrator.advanceToThinking(45_000);
    expect(deliver(narrator, 49_000).label).toBe('Thinking — taking a little longer…');
  });

  it('advanceToThinking is not swallowed by the sequence-based dedupe used in record()', () => {
    const narrator = new ProgressNarrator();
    narrator.start(seedLabelForInputKind('audio'), 0);
    deliver(narrator, 0);

    narrator.record({ phase: 'routing', action: 'completed', intent: 'read' }, 5);
    deliver(narrator, 4_000);

    narrator.advanceToThinking(8_000);
    expect(deliver(narrator, 8_000).label).toBe('Thinking…');
  });
});
