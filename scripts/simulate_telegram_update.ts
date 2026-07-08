import 'dotenv/config';

type SimulatorOptions = {
  text: string;
  userId: number;
  chatId: number;
  firstName: string;
  username?: string;
  baseUrl: string;
  secret: string;
  updateId: number;
  messageId: number;
};

type Environment = NodeJS.ProcessEnv;

const HELP = `Simulate an inbound Telegram text update through Jarvis's webhook.

Usage:
  npm run telegram:simulate -- [options] "message"

Options:
  --telegram-user-id <id>  Telegram identity to simulate
  --chat-id <id>           Chat receiving the simulated message (defaults to user id)
  --user-1                 Use JARVIS_CLI_USER_1_TELEGRAM_ID
  --user-2                 Use JARVIS_CLI_USER_2_TELEGRAM_ID
  --first-name <name>      Sender first name (default: CLI Tester)
  --username <name>        Optional Telegram username
  --base-url <url>         Running Jarvis URL (default: http://127.0.0.1:PORT)
  --secret <secret>        Webhook secret (default: TELEGRAM_SECRET_TOKEN)
  --update-id <id>         Stable Telegram update id (default: current timestamp)
  --message-id <id>        Telegram message id (default: update id)
  -h, --help               Show this help

This enters at POST /webhook/:secret and runs the real Telegram handlers, agent,
identity routing, integrations, and reply delivery. It does not pass through
Telegram's own servers and cannot impersonate a user to Telegram itself.`;

function requireValue(args: string[], index: number, flag: string): string {
  const value = args[index + 1];
  if (!value || value.startsWith('--')) {
    throw new Error(`${flag} requires a value.`);
  }
  return value;
}

function positiveInteger(value: string, flag: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new Error(`${flag} must be a positive integer.`);
  }
  return parsed;
}

export function parseSimulatorArgs(argv: string[], env: Environment = process.env): SimulatorOptions {
  let userId: number | undefined;
  let chatId: number | undefined;
  let firstName = 'CLI Tester';
  let username: string | undefined;
  let baseUrl = `http://127.0.0.1:${env.PORT || '3000'}`;
  let secret = env.TELEGRAM_SECRET_TOKEN;
  let updateId = Date.now();
  let messageId: number | undefined;
  let alias: 'user-1' | 'user-2' | undefined;
  const textParts: string[] = [];

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '-h' || arg === '--help') {
      throw new Error(HELP);
    }
    if (arg === '--user-1' || arg === '--user-2') {
      if (alias || userId) throw new Error('Choose only one Telegram user selector.');
      alias = arg.slice(2) as 'user-1' | 'user-2';
      continue;
    }
    if (arg === '--telegram-user-id') {
      if (alias || userId) throw new Error('Choose only one Telegram user selector.');
      userId = positiveInteger(requireValue(argv, index, arg), arg);
      index += 1;
      continue;
    }

    const valueFlags: Record<string, (value: string) => void> = {
      '--chat-id': (value) => {
        chatId = positiveInteger(value, '--chat-id');
      },
      '--first-name': (value) => {
        firstName = value;
      },
      '--username': (value) => {
        username = value.replace(/^@/, '');
      },
      '--base-url': (value) => {
        baseUrl = value;
      },
      '--secret': (value) => {
        secret = value;
      },
      '--update-id': (value) => {
        updateId = positiveInteger(value, '--update-id');
      },
      '--message-id': (value) => {
        messageId = positiveInteger(value, '--message-id');
      },
    };
    if (valueFlags[arg]) {
      valueFlags[arg](requireValue(argv, index, arg));
      index += 1;
      continue;
    }
    if (arg.startsWith('-')) throw new Error(`Unknown option: ${arg}`);
    textParts.push(arg);
  }

  if (alias) {
    const envName =
      alias === 'user-1' ? 'JARVIS_CLI_USER_1_TELEGRAM_ID' : 'JARVIS_CLI_USER_2_TELEGRAM_ID';
    if (!env[envName]) throw new Error(`${envName} is required for --${alias}.`);
    userId = positiveInteger(env[envName]!, envName);
  }
  if (!userId) {
    const defaultId = env.JARVIS_CLI_DEFAULT_TELEGRAM_ID;
    if (!defaultId) {
      throw new Error(
        'Choose --telegram-user-id, --user-1, or --user-2 (or set JARVIS_CLI_DEFAULT_TELEGRAM_ID).',
      );
    }
    userId = positiveInteger(defaultId, 'JARVIS_CLI_DEFAULT_TELEGRAM_ID');
  }
  if (!secret) throw new Error('Set TELEGRAM_SECRET_TOKEN or pass --secret.');
  const text = textParts.join(' ').trim();
  if (!text) throw new Error('Provide a message to simulate.');

  return {
    text,
    userId,
    chatId: chatId ?? userId,
    firstName,
    username,
    baseUrl: baseUrl.replace(/\/+$/, ''),
    secret,
    updateId,
    messageId: messageId ?? updateId,
  };
}

export function buildTelegramTextUpdate(options: SimulatorOptions) {
  return {
    update_id: options.updateId,
    message: {
      message_id: options.messageId,
      date: Math.floor(Date.now() / 1000),
      chat: { id: options.chatId, type: 'private' },
      from: {
        id: options.userId,
        is_bot: false,
        first_name: options.firstName,
        ...(options.username ? { username: options.username } : {}),
      },
      text: options.text,
    },
  };
}

export async function simulateTelegramUpdate(
  options: SimulatorOptions,
  fetcher: typeof fetch = fetch,
): Promise<void> {
  const url = `${options.baseUrl}/webhook/${encodeURIComponent(options.secret)}`;
  const response = await fetcher(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(buildTelegramTextUpdate(options)),
  });
  if (!response.ok) {
    throw new Error(`Webhook rejected the simulated update: HTTP ${response.status}.`);
  }
  console.log(`Accepted simulated Telegram update ${options.updateId} for user ${options.userId}.`);
  console.log('Jarvis processes webhook updates in the background; watch the server logs and chat.');
}

async function main() {
  try {
    const options = parseSimulatorArgs(process.argv.slice(2));
    await simulateTelegramUpdate(options);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const stream = message === HELP ? process.stdout : process.stderr;
    stream.write(`${message}\n`);
    process.exitCode = message === HELP ? 0 : 1;
  }
}

if (require.main === module) {
  void main();
}
