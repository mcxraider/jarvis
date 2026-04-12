import { CommandHandlers } from '../../../../../src/services/telegram/handlers/command-handlers';

describe('CommandHandlers', () => {
  function createContext() {
    return {
      from: { id: 123, username: 'tester' },
      reply: jest.fn().mockResolvedValue(undefined),
    } as any;
  }

  function createActivityService() {
    return {
      recordActivity: jest.fn(),
    } as any;
  }

  it('returns help text that only advertises /help and /status', async () => {
    const ctx = createContext();
    const activityService = createActivityService();
    const statusService = {
      getFormattedStatus: jest.fn(),
    } as any;
    const handlers = new CommandHandlers(activityService, statusService);

    await handlers.handleHelp(ctx);

    expect(activityService.recordActivity).toHaveBeenCalledWith('command_help');
    expect(ctx.reply).toHaveBeenCalledWith(expect.stringContaining('/help'), {
      parse_mode: 'Markdown',
    });
    expect(ctx.reply).toHaveBeenCalledWith(expect.stringContaining('/status'), {
      parse_mode: 'Markdown',
    });
    expect(ctx.reply).toHaveBeenCalledWith(expect.not.stringContaining('/start'), {
      parse_mode: 'Markdown',
    });
  });

  it('returns a formatted healthy status response', async () => {
    const ctx = createContext();
    const activityService = createActivityService();
    const statusService = {
      getFormattedStatus: jest.fn().mockResolvedValue('healthy status'),
    } as any;
    const handlers = new CommandHandlers(activityService, statusService);

    await handlers.handleStatus(ctx);

    expect(activityService.recordActivity).toHaveBeenCalledWith('command_status');
    expect(statusService.getFormattedStatus).toHaveBeenCalled();
    expect(ctx.reply).toHaveBeenCalledWith('healthy status', { parse_mode: 'Markdown' });
  });

  it('returns a formatted degraded status response without throwing', async () => {
    const ctx = createContext();
    const activityService = createActivityService();
    const statusService = {
      getFormattedStatus: jest.fn().mockResolvedValue('degraded status'),
    } as any;
    const handlers = new CommandHandlers(activityService, statusService);

    await handlers.handleStatus(ctx);

    expect(ctx.reply).toHaveBeenCalledWith('degraded status', { parse_mode: 'Markdown' });
  });
});
