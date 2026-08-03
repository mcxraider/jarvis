import { mkdtempSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join, resolve } from 'path';
import { spawnSync } from 'child_process';

const START_SCRIPT = resolve(__dirname, '../../../scripts/start_servers.sh');
const PROVIDER_ENV_KEYS = [
  'LLM_PROVIDER',
  'DEEPSEEK_API_KEY',
  'OPENAI_API_KEY',
  'LLM_SAFETY_IDENTIFIER_SECRET',
  'ROUTER_PROVIDER',
  'ROUTER_ENABLED',
  'TOOL_SELECTOR',
  'SUMMARIZER_PROVIDER',
  'LANGGRAPH_AGENT_URL',
];

describe('start_servers provider validation', () => {
  let directory: string;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), 'jarvis-start-test-'));
  });

  afterEach(() => {
    rmSync(directory, { recursive: true, force: true });
  });

  function checkEnv(contents: string) {
    const envFile = join(directory, '.env');
    writeFileSync(envFile, contents, 'utf8');
    const env = { ...process.env };
    for (const key of PROVIDER_ENV_KEYS) delete env[key];
    env.JARVIS_ENV_FILE = envFile;
    env.JARVIS_START_CHECK_ONLY = 'true';

    return spawnSync('bash', [START_SCRIPT], {
      cwd: directory,
      env,
      encoding: 'utf8',
    });
  }

  it('accepts DeepSeek without requiring an unused OpenAI key', () => {
    const result = checkEnv(
      'LANGGRAPH_AGENT_URL=http://localhost:8000\nLLM_PROVIDER=deepseek\nDEEPSEEK_API_KEY=test-deepseek\n',
    );

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('provider configuration valid (deepseek)');
  });

  it('accepts OpenAI without requiring an unused DeepSeek key', () => {
    const result = checkEnv(
      [
        'LANGGRAPH_AGENT_URL=http://localhost:8000',
        'LLM_PROVIDER= OPENAI ',
        'OPENAI_API_KEY=test-openai',
        'LLM_SAFETY_IDENTIFIER_SECRET=test-safety-secret',
        '',
      ].join('\n'),
    );

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('provider configuration valid (openai)');
  });

  it('defaults to OpenAI when LLM_PROVIDER is unset', () => {
    const result = checkEnv(
      [
        'LANGGRAPH_AGENT_URL=http://localhost:8000',
        'OPENAI_API_KEY=test-openai',
        'LLM_SAFETY_IDENTIFIER_SECRET=test-safety-secret',
        '',
      ].join('\n'),
    );

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('provider configuration valid (openai)');
  });

  it.each([
    [
      'DeepSeek key',
      'LANGGRAPH_AGENT_URL=http://localhost:8000\nLLM_PROVIDER=deepseek\n',
      'DEEPSEEK_API_KEY',
    ],
    [
      'OpenAI key',
      'LANGGRAPH_AGENT_URL=http://localhost:8000\nLLM_PROVIDER=openai\nLLM_SAFETY_IDENTIFIER_SECRET=test-secret\n',
      'OPENAI_API_KEY',
    ],
    [
      'OpenAI safety secret',
      'LANGGRAPH_AGENT_URL=http://localhost:8000\nLLM_PROVIDER=openai\nOPENAI_API_KEY=test-openai\n',
      'LLM_SAFETY_IDENTIFIER_SECRET',
    ],
  ])('rejects a missing selected %s', (_label, contents, missingKey) => {
    const result = checkEnv(contents);

    expect(result.status).toBe(1);
    expect(result.stdout).toContain(missingKey);
  });

  it('requires the key and safety secret selected by an enabled role override', () => {
    const result = checkEnv(
      [
        'LANGGRAPH_AGENT_URL=http://localhost:8000',
        'LLM_PROVIDER=deepseek',
        'DEEPSEEK_API_KEY=test-deepseek',
        'ROUTER_ENABLED=true',
        'TOOL_SELECTOR=router',
        'ROUTER_PROVIDER=openai',
        '',
      ].join('\n'),
    );

    expect(result.status).toBe(1);
    expect(result.stdout).toContain('OPENAI_API_KEY');
    expect(result.stdout).toContain('LLM_SAFETY_IDENTIFIER_SECRET');
  });

  it('ignores a router override while the router is disabled', () => {
    const result = checkEnv(
      [
        'LANGGRAPH_AGENT_URL=http://localhost:8000',
        'LLM_PROVIDER=deepseek',
        'DEEPSEEK_API_KEY=test-deepseek',
        'ROUTER_ENABLED=false',
        'ROUTER_PROVIDER=openai',
        '',
      ].join('\n'),
    );

    expect(result.status).toBe(0);
  });
});
