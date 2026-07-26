// Owns the commands displayed by Telegram's menu/autocomplete. Command handler
// registration is intentionally separate, so active commands do not have to be visible.

export interface TelegramMenuCommand {
  command: string;
  description: string;
}

export const DEFAULT_TELEGRAM_MENU_COMMANDS: readonly TelegramMenuCommand[] = [
  { command: 'new', description: 'Abandon the current step and start a new request' },
  { command: 'send_forward', description: 'Send buffered forwards to Jarvis with an instruction' },
  { command: 'cancel', description: 'Cancel the current operation' },
  { command: 'help', description: 'Show available commands and supported inputs' },
];

export class TelegramMenuRegistry {
  private readonly commands = new Map<string, TelegramMenuCommand>();

  constructor(initialCommands: readonly TelegramMenuCommand[] = DEFAULT_TELEGRAM_MENU_COMMANDS) {
    for (const command of initialCommands) {
      this.register(command);
    }
  }

  // Registration is intended for startup-time composition. Re-registering a command
  // updates its description without changing its position in the menu.
  register(command: TelegramMenuCommand): void {
    this.commands.set(command.command, { ...command });
  }

  getCommands(): TelegramMenuCommand[] {
    return Array.from(this.commands.values(), (command) => ({ ...command }));
  }
}
