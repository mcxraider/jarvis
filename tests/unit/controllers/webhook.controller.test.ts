import { createWebhookRouter } from '../../../src/controllers/webhook.controller';

describe('createWebhookRouter', () => {
  const originalSecret = process.env.TELEGRAM_SECRET_TOKEN;

  afterEach(() => {
    if (originalSecret === undefined) {
      delete process.env.TELEGRAM_SECRET_TOKEN;
    } else {
      process.env.TELEGRAM_SECRET_TOKEN = originalSecret;
    }
  });

  function getHandler(handleUpdate = jest.fn().mockResolvedValue(undefined)) {
    const botService = { handleUpdate } as any;
    const router = createWebhookRouter(botService) as any;
    const routeLayers = router.stack[0].route.stack;
    return {
      handleUpdate,
      handler: routeLayers[routeLayers.length - 1].handle,
    };
  }

  function createResponse() {
    return {
      statusCode: 200,
      body: undefined as unknown,
      sendStatus: jest.fn(function (this: any, status: number) {
        this.statusCode = status;
        return this;
      }),
      status: jest.fn(function (this: any, status: number) {
        this.statusCode = status;
        return this;
      }),
      json: jest.fn(function (this: any, body: unknown) {
        this.body = body;
        return this;
      }),
    };
  }

  it('rejects webhook requests with the wrong secret', async () => {
    process.env.TELEGRAM_SECRET_TOKEN = 'expected-secret';
    const { handler, handleUpdate } = getHandler();
    const response = createResponse();

    await handler(
      { params: { secret: 'wrong-secret' }, body: { update_id: 1 }, ip: '127.0.0.1' },
      response,
    );

    expect(response.sendStatus).toHaveBeenCalledWith(401);
    expect(handleUpdate).not.toHaveBeenCalled();
  });

  it('passes authorized updates to the bot service', async () => {
    process.env.TELEGRAM_SECRET_TOKEN = 'expected-secret';
    const { handler, handleUpdate } = getHandler();
    const update = { update_id: 42 };
    const response = createResponse();

    await handler(
      { params: { secret: 'expected-secret' }, body: update, ip: '127.0.0.1' },
      response,
    );

    expect(response.sendStatus).toHaveBeenCalledWith(200);
    expect(handleUpdate).toHaveBeenCalledWith({
      ...update,
      __requestId: 'tg_update_42',
    });
  });

  it('reuses the update id when Telegram redelivers an update', async () => {
    process.env.TELEGRAM_SECRET_TOKEN = 'expected-secret';
    const { handler, handleUpdate } = getHandler();

    for (let delivery = 0; delivery < 2; delivery += 1) {
      await handler(
        {
          params: { secret: 'expected-secret' },
          body: { update_id: 77 },
          ip: '127.0.0.1',
        },
        createResponse(),
      );
    }

    expect(handleUpdate).toHaveBeenCalledTimes(2);
    expect(handleUpdate.mock.calls.map(([update]) => update.__requestId)).toEqual([
      'tg_update_77',
      'tg_update_77',
    ]);
  });

  it('falls back to a generated id when update_id is unavailable', async () => {
    process.env.TELEGRAM_SECRET_TOKEN = 'expected-secret';
    const { handler, handleUpdate } = getHandler();

    await handler(
      { params: { secret: 'expected-secret' }, body: {}, ip: '127.0.0.1' },
      createResponse(),
    );

    expect(handleUpdate.mock.calls[0][0].__requestId).toMatch(/^tg_/);
  });

  it('returns 200 even when bot update handling fails (fire-and-forget)', async () => {
    process.env.TELEGRAM_SECRET_TOKEN = 'expected-secret';
    const { handler } = getHandler(jest.fn().mockRejectedValue(new Error('boom')));
    const response = createResponse();

    await handler(
      { params: { secret: 'expected-secret' }, body: { update_id: 99 }, ip: '127.0.0.1' },
      response,
    );

    expect(response.sendStatus).toHaveBeenCalledWith(200);
  });
});

describe('durable webhook acknowledgement', () => {
  function setup(inbox: any) {
    const bot = { handleUpdate: jest.fn() };
    const router = createWebhookRouter(bot as any, inbox) as any;
    const stack = router.stack[0].route.stack;
    const response: any = { sendStatus: jest.fn() };
    process.env.TELEGRAM_SECRET_TOKEN = 'secret';
    const req: any = { params: { secret: 'secret' }, body: { update_id: 7 }, get: () => 'secret' };
    return { handler: stack[stack.length - 1].handle, response, req, bot };
  }
  afterEach(() => { delete process.env.TELEGRAM_SECRET_TOKEN; });
  it('acknowledges only after the insert settles', async () => {
    let persist!: (accepted: boolean) => void;
    const inbox = { enqueue: jest.fn(() => new Promise<boolean>(resolve => { persist = resolve; })) };
    const { handler, response, req, bot } = setup(inbox);
    const handling = handler(req, response); expect(response.sendStatus).not.toHaveBeenCalled();
    persist(true); await handling; expect(response.sendStatus).toHaveBeenCalledWith(200);
    expect(bot.handleUpdate).not.toHaveBeenCalled();
  });
  it('returns 503 when persistence fails', async () => {
    const { handler, response, req } = setup({ enqueue: jest.fn().mockRejectedValue(new Error('offline')) });
    await handler(req, response); expect(response.sendStatus).toHaveBeenCalledWith(503);
  });
  it('rejects a missing Telegram secret header before storing content', async () => {
    const inbox = { enqueue: jest.fn() }; const { handler, response, req } = setup(inbox); req.get = () => undefined;
    await handler(req, response); expect(response.sendStatus).toHaveBeenCalledWith(401); expect(inbox.enqueue).not.toHaveBeenCalled();
  });
  it('rejects a missing update id', async () => {
    const inbox = { enqueue: jest.fn() }; const { handler, response, req } = setup(inbox); req.body = {};
    await handler(req, response); expect(response.sendStatus).toHaveBeenCalledWith(400); expect(inbox.enqueue).not.toHaveBeenCalled();
  });
});
