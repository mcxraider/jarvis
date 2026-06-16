import fs from 'fs';
import path from 'path';

type LogData = Record<string, unknown>;

interface LoggedEvent {
  timestamp: string;
  suite: string;
  testName?: string;
  eventType: string;
  name: string;
  data: unknown;
}

interface SummaryEntry {
  timestamp: string;
  type: string;
  name: string;
  data: unknown;
}

const SENSITIVE_KEY_PATTERN =
  /(authorization|token|secret|api[_-]?key|password|bot[_-]?token|openai[_-]?api[_-]?key|todoist[_-]?api[_-]?key|telegram[_-]?secret)/i;
const PRIVATE_ID_KEY_PATTERN = /^(chat_?id|user_?id|from_?id|telegram_?user_?id)$/i;

function getTimestamp(): string {
  return new Date().toISOString().replace(/[:.]/g, '-');
}

function redact(value: unknown, key = ''): unknown {
  if (SENSITIVE_KEY_PATTERN.test(key)) {
    return '[REDACTED]';
  }

  if (PRIVATE_ID_KEY_PATTERN.test(key)) {
    return '[REDACTED_ID]';
  }

  if (typeof value === 'string') {
    if (/^Bearer\s+/i.test(value)) {
      return 'Bearer [REDACTED]';
    }

    if (value.includes('api.telegram.org/file/bot')) {
      return '[REDACTED_TELEGRAM_FILE_URL]';
    }

    return value;
  }

  if (Array.isArray(value)) {
    return value.map((item) => redact(item, key));
  }

  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([entryKey, entryValue]) => [
        entryKey,
        redact(entryValue, entryKey),
      ]),
    );
  }

  return value;
}

export class TestRunLogger {
  private readonly eventsPath: string;
  private readonly summaryPath: string;
  private readonly artifactsDir: string;
  private readonly entries: SummaryEntry[] = [];

  constructor(private readonly suite: string) {
    const runDir = path.join(process.cwd(), 'logs', 'test-runs', `${getTimestamp()}-${suite}`);
    this.eventsPath = path.join(runDir, 'events.jsonl');
    this.summaryPath = path.join(runDir, 'summary.md');
    this.artifactsDir = path.join(runDir, 'artifacts');

    fs.mkdirSync(this.artifactsDir, { recursive: true });
    fs.writeFileSync(this.eventsPath, '');
    fs.writeFileSync(this.summaryPath, `# ${suite}\n\nStarted: ${new Date().toISOString()}\n`);
  }

  getArtifactsDir(): string {
    return this.artifactsDir;
  }

  logStep(name: string, data: LogData = {}): void {
    this.log('step', name, data);
  }

  logRequest(name: string, data: LogData): void {
    this.log('request', name, data);
  }

  logResponse(name: string, data: LogData): void {
    this.log('response', name, data);
  }

  logAssertion(name: string, data: LogData): void {
    this.log('assertion', name, data);
  }

  writeArtifact(name: string, data: unknown): string {
    const safeName = name.replace(/[^a-z0-9._-]/gi, '-').toLowerCase();
    const fileName = safeName.endsWith('.json') ? safeName : `${safeName}.json`;
    const filePath = path.join(this.artifactsDir, fileName);

    fs.writeFileSync(filePath, `${JSON.stringify(redact(data), null, 2)}\n`);
    this.logStep('artifact_written', { name, path: path.relative(process.cwd(), filePath) });

    return filePath;
  }

  writeSummary(status = 'completed'): void {
    const lines = [
      `# ${this.suite}`,
      '',
      `Status: ${status}`,
      `Finished: ${new Date().toISOString()}`,
      '',
      '## Events',
      '',
      ...this.entries.map((entry) => {
        const data = JSON.stringify(entry.data);
        return `- ${entry.timestamp} [${entry.type}] ${entry.name}${data === '{}' ? '' : `: ${data}`}`;
      }),
      '',
    ];

    fs.writeFileSync(this.summaryPath, lines.join('\n'));
  }

  private log(eventType: string, name: string, data: LogData): void {
    const event: LoggedEvent = {
      timestamp: new Date().toISOString(),
      suite: this.suite,
      testName: expect.getState().currentTestName,
      eventType,
      name,
      data: redact(data),
    };

    this.entries.push({
      timestamp: event.timestamp,
      type: eventType,
      name,
      data: event.data,
    });

    fs.appendFileSync(this.eventsPath, `${JSON.stringify(event)}\n`);
  }
}

export function createTestRunLogger(suite: string): TestRunLogger {
  return new TestRunLogger(suite);
}
