import {
  BotActivityService,
  BotActivityType,
  BotActivitySnapshot,
} from '../../../../src/services/telegram/bot-activity.service';

describe('BotActivityService', () => {
  let service: BotActivityService;

  beforeEach(() => {
    jest.useFakeTimers();
    service = new BotActivityService();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  describe('initial state', () => {
    it('starts with zero total interactions', () => {
      const snapshot = service.getSnapshot();
      expect(snapshot.totalInteractions).toBe(0);
    });

    it('starts with null lastActivityAt', () => {
      const snapshot = service.getSnapshot();
      expect(snapshot.lastActivityAt).toBeNull();
    });

    it('starts with null lastActivityType', () => {
      const snapshot = service.getSnapshot();
      expect(snapshot.lastActivityType).toBeNull();
    });

    it('reports uptimeMs >= 0', () => {
      const snapshot = service.getSnapshot();
      expect(snapshot.uptimeMs).toBeGreaterThanOrEqual(0);
    });

    it('reports startedAt as a Date instance', () => {
      const snapshot = service.getSnapshot();
      expect(snapshot.startedAt).toBeInstanceOf(Date);
    });
  });

  describe('recordActivity', () => {
    it('increments totalInteractions by 1', () => {
      service.recordActivity('message_text');
      const snapshot = service.getSnapshot();
      expect(snapshot.totalInteractions).toBe(1);
    });

    it('sets lastActivityType to the given type', () => {
      service.recordActivity('message_voice');
      const snapshot = service.getSnapshot();
      expect(snapshot.lastActivityType).toBe('message_voice');
    });

    it('sets lastActivityAt to current time', () => {
      const now = new Date();
      service.recordActivity('command_help');
      const snapshot = service.getSnapshot();
      expect(snapshot.lastActivityAt).toEqual(now);
    });

    it('increments correctly across multiple calls', () => {
      service.recordActivity('message_text');
      service.recordActivity('message_voice');
      service.recordActivity('command_status');
      const snapshot = service.getSnapshot();
      expect(snapshot.totalInteractions).toBe(3);
    });

    it('tracks the most recent activity type (overwrites previous)', () => {
      service.recordActivity('message_text');
      service.recordActivity('message_voice');
      service.recordActivity('command_help');
      const snapshot = service.getSnapshot();
      expect(snapshot.lastActivityType).toBe('command_help');
    });
  });

  describe('getSnapshot', () => {
    it('returns a consistent snapshot object', () => {
      service.recordActivity('message_text');
      const snapshot = service.getSnapshot();

      expect(snapshot).toEqual<BotActivitySnapshot>({
        startedAt: expect.any(Date),
        uptimeMs: expect.any(Number),
        totalInteractions: 1,
        lastActivityAt: expect.any(Date),
        lastActivityType: 'message_text',
      });
    });

    it('uptimeMs increases over time', () => {
      const snapshot1 = service.getSnapshot();
      jest.advanceTimersByTime(5000);
      const snapshot2 = service.getSnapshot();

      expect(snapshot2.uptimeMs - snapshot1.uptimeMs).toBe(5000);
    });

    it('reflects all recorded activities', () => {
      service.recordActivity('command_help');
      jest.advanceTimersByTime(100);
      service.recordActivity('message_text');
      jest.advanceTimersByTime(100);
      service.recordActivity('message_voice');

      const snapshot = service.getSnapshot();
      expect(snapshot.totalInteractions).toBe(3);
      expect(snapshot.lastActivityType).toBe('message_voice');
      expect(snapshot.lastActivityAt).not.toBeNull();
    });
  });

  describe('all activity types', () => {
    it('accepts every BotActivityType value', () => {
      const allTypes: BotActivityType[] = [
        'command_help',
        'command_status',
        'message_text',
        'message_voice',
        'message_audio',
        'message_photo',
        'message_document',
        'message_unknown',
      ];

      allTypes.forEach((type) => {
        service.recordActivity(type);
      });

      const snapshot = service.getSnapshot();
      expect(snapshot.totalInteractions).toBe(allTypes.length);
      expect(snapshot.lastActivityType).toBe('message_unknown');
    });
  });

  describe('edge cases', () => {
    it('uptimeMs is zero immediately after creation', () => {
      const snapshot = service.getSnapshot();
      expect(snapshot.uptimeMs).toBe(0);
    });

    it('uptimeMs advances with fake timers', () => {
      jest.advanceTimersByTime(12345);
      const snapshot = service.getSnapshot();
      expect(snapshot.uptimeMs).toBe(12345);
    });

    it('lastActivityAt updates to the time of each call', () => {
      service.recordActivity('message_text');
      const firstTime = service.getSnapshot().lastActivityAt;

      jest.advanceTimersByTime(3000);
      service.recordActivity('message_voice');
      const secondTime = service.getSnapshot().lastActivityAt;

      expect(secondTime!.getTime() - firstTime!.getTime()).toBe(3000);
    });

    it('calling recordActivity with different types in sequence always reflects the latest', () => {
      const types: BotActivityType[] = [
        'command_help',
        'message_text',
        'message_audio',
        'command_status',
        'message_photo',
      ];

      types.forEach((type, index) => {
        service.recordActivity(type);
        const snapshot = service.getSnapshot();
        expect(snapshot.lastActivityType).toBe(type);
        expect(snapshot.totalInteractions).toBe(index + 1);
      });
    });
  });
});
