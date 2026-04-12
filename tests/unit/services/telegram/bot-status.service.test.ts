import { BotActivityService } from '../../../../src/services/telegram/bot-activity.service';
import { BotStatusService } from '../../../../src/services/telegram/bot-status.service';

describe('BotStatusService', () => {
  it('reports a healthy runtime when Todoist is reachable', async () => {
    const activity = new BotActivityService();
    activity.recordActivity('message_text');

    const service = new BotStatusService(activity, {
      gptModel: 'gpt-4o',
      todoistService: {
        getProjects: jest.fn().mockResolvedValue([{ id: '1' }]),
      } as any,
    });

    const status = await service.getFormattedStatus();

    expect(status).toContain('HEALTHY');
    expect(status).toContain('GPT model: gpt-4o');
    expect(status).toContain('Todoist: reachable');
    expect(status).toContain('Total interactions: 1');
    expect(status).toContain('Last activity type: message_text');
  });

  it('reports degraded dependency health when Todoist check fails', async () => {
    const activity = new BotActivityService();
    const service = new BotStatusService(activity, {
      todoistService: {
        getProjects: jest.fn().mockRejectedValue(new Error('Todoist API error (401): unauthorized')),
      } as any,
    });

    const status = await service.getFormattedStatus();

    expect(status).toContain('DEGRADED');
    expect(status).toContain('Todoist: degraded');
    expect(status).toContain('unauthorized');
    expect(status).toContain('Last activity: none yet');
  });
});
