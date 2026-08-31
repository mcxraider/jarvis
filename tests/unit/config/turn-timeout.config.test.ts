import {
  assertTurnTimeoutLadder,
  findTurnTimeoutLadderViolations,
  resolveAudioPrepareTimeoutMs,
  resolveAudioTranscriptionTimeoutMs,
  resolveLangGraphClientTimeouts,
  resolveRunningGateTtlMs,
  resolveTelegrafHandlerTimeoutMs,
  resolveTurnTimeoutConfig,
  resolveWaitingGateTtlMs,
  TURN_TIMEOUT_DEFAULTS,
  TurnTimeoutConfig,
  TurnTimeoutOverrides,
} from '../../../src/config/turn-timeout.config';

const DEFAULT_LADDER: TurnTimeoutConfig = {
  overallMs: 165_000,
  streamIdleMs: 155_000,
  telegrafHandlerMs: 600_000,
  audioPrepareMs: 120_000,
  audioTranscriptionMs: 360_000,
  runningGateTtlMs: 720_000,
  waitingGateTtlMs: 1_800_000,
};

describe('turn timeout configuration', () => {
  it('uses the code-owned ladder defaults when no overrides are present', () => {
    expect(resolveTurnTimeoutConfig({}, {})).toEqual(DEFAULT_LADDER);
    expect(TURN_TIMEOUT_DEFAULTS).toEqual(DEFAULT_LADDER);
  });

  it('keeps the long-audio ladder numbers pinned', () => {
    // These are contract numbers, not tuning knobs. Assert each one explicitly so
    // a silent regression in any single stage budget fails here.
    expect(TURN_TIMEOUT_DEFAULTS.overallMs).toBe(165_000);
    expect(TURN_TIMEOUT_DEFAULTS.streamIdleMs).toBe(155_000);
    expect(TURN_TIMEOUT_DEFAULTS.telegrafHandlerMs).toBe(600_000);
    expect(TURN_TIMEOUT_DEFAULTS.audioPrepareMs).toBe(120_000);
    expect(TURN_TIMEOUT_DEFAULTS.audioTranscriptionMs).toBe(360_000);
    expect(TURN_TIMEOUT_DEFAULTS.runningGateTtlMs).toBe(720_000);
    expect(TURN_TIMEOUT_DEFAULTS.waitingGateTtlMs).toBe(1_800_000);
  });

  it('accepts optional environment overrides for emergency and test use', () => {
    const env = {
      LANGGRAPH_AGENT_TIMEOUT_MS: '180000',
      LANGGRAPH_STREAM_IDLE_TIMEOUT_MS: '130000',
      TELEGRAM_HANDLER_TIMEOUT_MS: '610000',
    };

    expect(resolveTurnTimeoutConfig({}, env)).toEqual({
      ...DEFAULT_LADDER,
      overallMs: 180_000,
      streamIdleMs: 130_000,
      telegrafHandlerMs: 610_000,
    });
  });

  it('accepts a consistent full set of environment overrides', () => {
    const env = {
      LANGGRAPH_AGENT_TIMEOUT_MS: '200000',
      LANGGRAPH_STREAM_IDLE_TIMEOUT_MS: '190000',
      TELEGRAM_HANDLER_TIMEOUT_MS: '900000',
      GROQ_AUDIO_PREPARE_TIMEOUT_MS: '150000',
      GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS: '600000',
      TELEGRAM_GATE_RUNNING_TTL_MS: '960000',
      TELEGRAM_GATE_WAITING_TTL_MS: '2400000',
    };

    expect(resolveTurnTimeoutConfig({}, env)).toEqual({
      overallMs: 200_000,
      streamIdleMs: 190_000,
      telegrafHandlerMs: 900_000,
      audioPrepareMs: 150_000,
      audioTranscriptionMs: 600_000,
      runningGateTtlMs: 960_000,
      waitingGateTtlMs: 2_400_000,
    });
  });

  it('gives explicit constructor and service values precedence over environment overrides', () => {
    const invalidEnvironment = {
      LANGGRAPH_AGENT_TIMEOUT_MS: 'invalid',
      LANGGRAPH_STREAM_IDLE_TIMEOUT_MS: '0',
      TELEGRAM_HANDLER_TIMEOUT_MS: '-1',
      GROQ_AUDIO_PREPARE_TIMEOUT_MS: 'abc',
      GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS: '',
      TELEGRAM_GATE_RUNNING_TTL_MS: 'Infinity',
      TELEGRAM_GATE_WAITING_TTL_MS: '0',
    };

    expect(
      resolveTurnTimeoutConfig(
        {
          timeoutMs: 170_000,
          streamIdleTimeoutMs: 125_000,
          handlerTimeoutMs: 620_000,
          audioPrepareMs: 100_000,
          audioTranscriptionMs: 300_000,
          runningGateTtlMs: 700_000,
          waitingGateTtlMs: 1_500_000,
        },
        invalidEnvironment,
      ),
    ).toEqual({
      overallMs: 170_000,
      streamIdleMs: 125_000,
      telegrafHandlerMs: 620_000,
      audioPrepareMs: 100_000,
      audioTranscriptionMs: 300_000,
      runningGateTtlMs: 700_000,
      waitingGateTtlMs: 1_500_000,
    });
  });

  it.each([
    ['LANGGRAPH_AGENT_TIMEOUT_MS', '0'],
    ['LANGGRAPH_AGENT_TIMEOUT_MS', '-1'],
    ['LANGGRAPH_AGENT_TIMEOUT_MS', 'nan'],
    ['LANGGRAPH_AGENT_TIMEOUT_MS', 'Infinity'],
    ['LANGGRAPH_STREAM_IDLE_TIMEOUT_MS', '0'],
    ['LANGGRAPH_STREAM_IDLE_TIMEOUT_MS', '-1'],
    ['LANGGRAPH_STREAM_IDLE_TIMEOUT_MS', 'nan'],
    ['LANGGRAPH_STREAM_IDLE_TIMEOUT_MS', 'Infinity'],
    ['TELEGRAM_HANDLER_TIMEOUT_MS', '0'],
    ['TELEGRAM_HANDLER_TIMEOUT_MS', '-1'],
    ['TELEGRAM_HANDLER_TIMEOUT_MS', 'nan'],
    ['TELEGRAM_HANDLER_TIMEOUT_MS', 'Infinity'],
    ['GROQ_AUDIO_PREPARE_TIMEOUT_MS', '0'],
    ['GROQ_AUDIO_PREPARE_TIMEOUT_MS', '-1'],
    ['GROQ_AUDIO_PREPARE_TIMEOUT_MS', 'abc'],
    ['GROQ_AUDIO_PREPARE_TIMEOUT_MS', ''],
    ['GROQ_AUDIO_PREPARE_TIMEOUT_MS', 'Infinity'],
    ['GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS', '0'],
    ['GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS', '-1'],
    ['GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS', 'abc'],
    ['GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS', ''],
    ['GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS', 'Infinity'],
    ['TELEGRAM_GATE_RUNNING_TTL_MS', '0'],
    ['TELEGRAM_GATE_RUNNING_TTL_MS', '-1'],
    ['TELEGRAM_GATE_RUNNING_TTL_MS', 'abc'],
    ['TELEGRAM_GATE_RUNNING_TTL_MS', ''],
    ['TELEGRAM_GATE_RUNNING_TTL_MS', 'Infinity'],
    ['TELEGRAM_GATE_WAITING_TTL_MS', '0'],
    ['TELEGRAM_GATE_WAITING_TTL_MS', '-1'],
    ['TELEGRAM_GATE_WAITING_TTL_MS', 'abc'],
    ['TELEGRAM_GATE_WAITING_TTL_MS', ''],
    ['TELEGRAM_GATE_WAITING_TTL_MS', 'Infinity'],
  ])('rejects invalid override %s=%s', (name, value) => {
    expect(() => resolveTurnTimeoutConfig({}, { [name]: value })).toThrow(
      `${name} must be finite and greater than zero`,
    );
  });

  it.each<[keyof TurnTimeoutOverrides, number]>([
    ['timeoutMs', 0],
    ['streamIdleTimeoutMs', -1],
    ['handlerTimeoutMs', Number.NaN],
    ['audioPrepareMs', 0],
    ['audioPrepareMs', -1],
    ['audioPrepareMs', Number.NaN],
    ['audioPrepareMs', Number.POSITIVE_INFINITY],
    ['audioTranscriptionMs', 0],
    ['audioTranscriptionMs', -1],
    ['audioTranscriptionMs', Number.NaN],
    ['audioTranscriptionMs', Number.POSITIVE_INFINITY],
    ['runningGateTtlMs', 0],
    ['runningGateTtlMs', -1],
    ['runningGateTtlMs', Number.NaN],
    ['runningGateTtlMs', Number.POSITIVE_INFINITY],
    ['waitingGateTtlMs', 0],
    ['waitingGateTtlMs', -1],
    ['waitingGateTtlMs', Number.NaN],
    ['waitingGateTtlMs', Number.POSITIVE_INFINITY],
  ])('rejects invalid explicit override %s=%s', (name, value) => {
    expect(() => resolveTurnTimeoutConfig({ [name]: value }, {})).toThrow(
      `${name} must be finite and greater than zero`,
    );
  });

  it('lets standalone owners resolve only their part of the ladder', () => {
    expect(resolveLangGraphClientTimeouts({}, {})).toEqual({
      overallMs: 165_000,
      streamIdleMs: 155_000,
    });
    expect(resolveTelegrafHandlerTimeoutMs(undefined, {})).toBe(600_000);
  });

  describe('single-value resolvers', () => {
    const cases: Array<{
      name: string;
      resolve: (explicit?: number, env?: NodeJS.ProcessEnv) => number;
      envName: string;
      explicitName: string;
      defaultValue: number;
    }> = [
      {
        name: 'audio prepare',
        resolve: resolveAudioPrepareTimeoutMs,
        envName: 'GROQ_AUDIO_PREPARE_TIMEOUT_MS',
        explicitName: 'audioPrepareMs',
        defaultValue: 120_000,
      },
      {
        name: 'audio transcription',
        resolve: resolveAudioTranscriptionTimeoutMs,
        envName: 'GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS',
        explicitName: 'audioTranscriptionMs',
        defaultValue: 360_000,
      },
      {
        name: 'running gate TTL',
        resolve: resolveRunningGateTtlMs,
        envName: 'TELEGRAM_GATE_RUNNING_TTL_MS',
        explicitName: 'runningGateTtlMs',
        defaultValue: 720_000,
      },
      {
        name: 'waiting gate TTL',
        resolve: resolveWaitingGateTtlMs,
        envName: 'TELEGRAM_GATE_WAITING_TTL_MS',
        explicitName: 'waitingGateTtlMs',
        defaultValue: 1_800_000,
      },
    ];

    it.each(cases)('$name honours explicit > env > default', (testCase) => {
      expect(testCase.resolve(undefined, {})).toBe(testCase.defaultValue);
      expect(testCase.resolve(undefined, { [testCase.envName]: '11000' })).toBe(11_000);
      expect(testCase.resolve(12_000, { [testCase.envName]: '11000' })).toBe(12_000);
    });

    it.each(cases)('$name rejects an invalid environment value', (testCase) => {
      for (const value of ['0', '-1', 'abc', '', 'Infinity']) {
        expect(() => testCase.resolve(undefined, { [testCase.envName]: value })).toThrow(
          `${testCase.envName} must be finite and greater than zero`,
        );
      }
    });

    it.each(cases)('$name rejects an invalid explicit value', (testCase) => {
      for (const value of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
        expect(() => testCase.resolve(value, {})).toThrow(
          `${testCase.explicitName} must be finite and greater than zero`,
        );
      }
    });
  });

  describe('ladder invariants', () => {
    it('reports no violations for the production defaults', () => {
      expect(findTurnTimeoutLadderViolations(DEFAULT_LADDER)).toEqual([]);
      expect(() => assertTurnTimeoutLadder(DEFAULT_LADDER)).not.toThrow();
    });

    it('holds the whole production chain in one expression', () => {
      const config = resolveTurnTimeoutConfig({}, {});
      expect(
        config.streamIdleMs < config.overallMs &&
          config.overallMs < config.telegrafHandlerMs &&
          config.telegrafHandlerMs < config.runningGateTtlMs &&
          config.runningGateTtlMs <= config.waitingGateTtlMs,
      ).toBe(true);
      expect(config.audioPrepareMs + config.audioTranscriptionMs).toBeLessThan(
        config.telegrafHandlerMs,
      );
    });

    it.each([
      ['streamIdleMs < overallMs', { streamIdleMs: 200_000 }],
      ['streamIdleMs < overallMs', { streamIdleMs: 165_000 }],
      ['overallMs < telegrafHandlerMs', { overallMs: 700_000, streamIdleMs: 1_000 }],
      ['overallMs < telegrafHandlerMs', { overallMs: 600_000, streamIdleMs: 1_000 }],
      [
        'audioPrepareMs + audioTranscriptionMs < telegrafHandlerMs',
        { audioTranscriptionMs: 500_000 },
      ],
      [
        'audioPrepareMs + audioTranscriptionMs < telegrafHandlerMs',
        { audioTranscriptionMs: 480_000 },
      ],
      ['telegrafHandlerMs < runningGateTtlMs', { runningGateTtlMs: 500_000 }],
      ['telegrafHandlerMs < runningGateTtlMs', { runningGateTtlMs: 600_000 }],
      ['runningGateTtlMs <= waitingGateTtlMs', { waitingGateTtlMs: 700_000 }],
    ])('flags exactly one violation for %s with %o', (rule, patch) => {
      const violations = findTurnTimeoutLadderViolations({ ...DEFAULT_LADDER, ...patch });

      expect(violations).toHaveLength(1);
      expect(violations[0].rule).toBe(rule);
      expect(violations[0].detail).toEqual(expect.any(String));
    });

    it('treats equal running and waiting gate TTLs as valid', () => {
      expect(
        findTurnTimeoutLadderViolations({ ...DEFAULT_LADDER, waitingGateTtlMs: 720_000 }),
      ).toEqual([]);
    });

    it('lists every violation when several rules break at once', () => {
      const broken: TurnTimeoutConfig = {
        overallMs: 100_000,
        streamIdleMs: 150_000,
        telegrafHandlerMs: 90_000,
        audioPrepareMs: 60_000,
        audioTranscriptionMs: 60_000,
        runningGateTtlMs: 80_000,
        waitingGateTtlMs: 70_000,
      };

      expect(findTurnTimeoutLadderViolations(broken).map((violation) => violation.rule)).toEqual([
        'streamIdleMs < overallMs',
        'overallMs < telegrafHandlerMs',
        'audioPrepareMs + audioTranscriptionMs < telegrafHandlerMs',
        'telegrafHandlerMs < runningGateTtlMs',
        'runningGateTtlMs <= waitingGateTtlMs',
      ]);

      let message = '';
      try {
        assertTurnTimeoutLadder(broken);
      } catch (error) {
        message = error instanceof Error ? error.message : String(error);
      }

      expect(message).toContain('Turn timeout ladder inverted:');
      for (const rule of [
        'streamIdleMs < overallMs',
        'overallMs < telegrafHandlerMs',
        'audioPrepareMs + audioTranscriptionMs < telegrafHandlerMs',
        'telegrafHandlerMs < runningGateTtlMs',
        'runningGateTtlMs <= waitingGateTtlMs',
      ]) {
        expect(message).toContain(rule);
      }
    });

    it('fails fast at resolution time when an environment override inverts the ladder', () => {
      expect(() => resolveTurnTimeoutConfig({}, { TELEGRAM_GATE_RUNNING_TTL_MS: '1000' })).toThrow(
        'Turn timeout ladder inverted: telegrafHandlerMs < runningGateTtlMs: telegrafHandlerMs 600000ms must be below runningGateTtlMs 1000ms',
      );
    });

    it('fails fast when a shortened Telegraf handler cannot fit the audio stage', () => {
      expect(() => resolveTurnTimeoutConfig({}, { TELEGRAM_HANDLER_TIMEOUT_MS: '300000' })).toThrow(
        'Turn timeout ladder inverted: audioPrepareMs + audioTranscriptionMs < telegrafHandlerMs: audio stage 480000ms (prepare 120000ms + transcription 360000ms) must be below telegrafHandlerMs 300000ms',
      );
    });
  });
});
