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
    expect(handleUpdate).toHaveBeenCalledWith(update);
  });

  it('returns 500 when bot update handling fails', async () => {
    process.env.TELEGRAM_SECRET_TOKEN = 'expected-secret';
    const { handler } = getHandler(jest.fn().mockRejectedValue(new Error('boom')));
    const response = createResponse();

    await handler(
      { params: { secret: 'expected-secret' }, body: { update_id: 99 }, ip: '127.0.0.1' },
      response,
    );

    expect(response.status).toHaveBeenCalledWith(500);
    expect(response.json).toHaveBeenCalledWith({ error: 'Internal Server Error' });
  });
});
