import {
  TelegramMenuRegistry,
} from '../../../../src/services/telegram/telegram-menu.registry';

describe('TelegramMenuRegistry', () => {
  it('exposes only the default visible commands in order', () => {
    const registry = new TelegramMenuRegistry();

    expect(registry.getCommands()).toEqual([
      { command: 'new', description: 'Abandon the current step and start a new request' },
      { command: 'cancel', description: 'Cancel the current operation' },
      { command: 'help', description: 'Show available commands and supported inputs' },
    ]);
  });

  it('registers additional commands without exposing mutable internal state', () => {
    const registry = new TelegramMenuRegistry();
    registry.register({ command: 'schedule_next_week', description: 'Show next week schedule' });

    const commands = registry.getCommands();
    commands[0].description = 'changed externally';

    expect(registry.getCommands()).toEqual([
      { command: 'new', description: 'Abandon the current step and start a new request' },
      { command: 'cancel', description: 'Cancel the current operation' },
      { command: 'help', description: 'Show available commands and supported inputs' },
      { command: 'schedule_next_week', description: 'Show next week schedule' },
    ]);
  });
});
