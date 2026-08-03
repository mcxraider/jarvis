import {
  createFlowHarness,
  createMockAgentClient,
  createCallbackCtx,
  interruptResponse,
  completedResponse,
  failedResponse,
} from './helpers';

describe('Confirm flow: text → buttons → callback → reply', () => {
  const THREAD_ID = 'tg_confirm_thread_001';

  it('full approve flow delivers final response after button press', async () => {
    const agentClient = createMockAgentClient({
      invoke: [interruptResponse({ threadId: THREAD_ID, message: '⚠️ Delete 5 tasks?' })],
      resume: [completedResponse({ threadId: THREAD_ID, message: 'Done. 5 tasks deleted.' })],
    });
    const harness = createFlowHarness(agentClient);

    const { result } = await harness.sendText('delete all tasks on tuesday');
    expect(result.interruptType).toBe('confirm');
    expect(result.threadId).toBe(THREAD_ID);

    const { ctx } = await harness.pressButton(`confirm:approve:${THREAD_ID}`);
    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'approve', threadId: THREAD_ID }),
      expect.any(Object),
      expect.any(Function),
    );
    expect(ctx.reply).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ parse_mode: 'MarkdownV2' }),
    );
  });

  it('full decline flow delivers final response after button press', async () => {
    const agentClient = createMockAgentClient({
      invoke: [interruptResponse({ threadId: THREAD_ID, message: '⚠️ Delete 5 tasks?' })],
      resume: [completedResponse({ threadId: THREAD_ID, message: 'Action declined.' })],
    });
    const harness = createFlowHarness(agentClient);

    await harness.sendText('delete all tasks on tuesday');
    const { ctx } = await harness.pressButton(`confirm:decline:${THREAD_ID}`);

    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'decline', threadId: THREAD_ID }),
      expect.any(Object),
      expect.any(Function),
    );
    expect(ctx.answerCbQuery).toHaveBeenCalledWith('Declined.');
  });

  it('chained confirm: first approve triggers second interrupt with new buttons', async () => {
    const secondThreadId = 'tg_confirm_thread_002';
    const agentClient = createMockAgentClient({
      invoke: [interruptResponse({ threadId: THREAD_ID, message: 'Confirm step 1?' })],
      resume: [
        interruptResponse({ threadId: secondThreadId, message: 'Now confirm step 2?' }),
        completedResponse({ threadId: secondThreadId, message: 'All done.' }),
      ],
    });
    const harness = createFlowHarness(agentClient);

    await harness.sendText('do multi-step action');

    const { ctx: firstCtx } = await harness.pressButton(`confirm:approve:${THREAD_ID}`);
    expect(firstCtx.reply).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        reply_markup: expect.objectContaining({
          inline_keyboard: expect.arrayContaining([
            expect.arrayContaining([
              expect.objectContaining({ callback_data: `confirm:approve:${secondThreadId}` }),
              expect.objectContaining({ callback_data: `confirm:decline:${secondThreadId}` }),
            ]),
          ]),
        }),
      }),
    );

    const { ctx: secondCtx } = await harness.pressButton(`confirm:approve:${secondThreadId}`);
    expect(secondCtx.reply).toHaveBeenCalledWith(
      expect.stringContaining('All done'),
      expect.objectContaining({ parse_mode: 'MarkdownV2' }),
    );
  });

  it('callback after resume failure shows error and clears pending state', async () => {
    const agentClient = createMockAgentClient({
      invoke: [interruptResponse({ threadId: THREAD_ID, message: 'Confirm?' })],
      resume: [failedResponse({ threadId: THREAD_ID, error: 'timeout' })],
    });
    const harness = createFlowHarness(agentClient);

    await harness.sendText('risky action');
    const { ctx } = await harness.pressButton(`confirm:approve:${THREAD_ID}`);

    expect(ctx.reply).toHaveBeenCalledWith(
      expect.stringContaining('unavailable'),
      expect.anything(),
    );

    const record = await harness.store.get(expect.any(String) as any);
    expect(record).toBeUndefined();
  });

  it('resume throwing an exception shows a generic error to the user', async () => {
    const agentClient = createMockAgentClient({
      invoke: [interruptResponse({ threadId: THREAD_ID, message: 'Confirm?' })],
    });
    agentClient.resume.mockRejectedValueOnce(new Error('network failure'));
    const harness = createFlowHarness(agentClient);

    await harness.sendText('risky action');
    const { ctx } = await harness.pressButton(`confirm:approve:${THREAD_ID}`);

    expect(ctx.reply).toHaveBeenCalledWith(
      'Something went wrong processing your decision. Please try again.',
    );
  });

  it('typing text while confirm pending returns warning (buttons still active)', async () => {
    const agentClient = createMockAgentClient({
      invoke: [interruptResponse({ threadId: THREAD_ID, message: 'Confirm delete?' })],
    });
    const harness = createFlowHarness(agentClient);

    const { result: firstResult } = await harness.sendText('delete tasks');
    expect(firstResult.interruptType).toBe('confirm');

    const { result: secondResult } = await harness.sendText('add buy milk', { messageId: 2 });
    expect(secondResult.response).toMatch(/pending approval/i);
    expect(agentClient.invoke).toHaveBeenCalledTimes(1);
    expect(agentClient.resume).not.toHaveBeenCalled();
  });

  it('approve callback removes the prompt and posts the decision as a new message', async () => {
    const agentClient = createMockAgentClient({
      invoke: [interruptResponse({ threadId: THREAD_ID, message: 'Confirm?' })],
      resume: [completedResponse({ threadId: THREAD_ID, message: 'Done.' })],
    });
    const harness = createFlowHarness(agentClient);

    await harness.sendText('action');
    const { ctx } = await harness.pressButton(`confirm:approve:${THREAD_ID}`, {
      originalText: '⚠️ Confirm: Delete tasks',
    } as any);

    expect(ctx.reply).toHaveBeenCalledWith('✅ Approved', { parse_mode: 'MarkdownV2' });
    expect(ctx.editMessageText).not.toHaveBeenCalled();
  });

  it('decline callback removes the prompt and posts the decision as a new message', async () => {
    const agentClient = createMockAgentClient({
      invoke: [interruptResponse({ threadId: THREAD_ID, message: 'Confirm?' })],
      resume: [completedResponse({ threadId: THREAD_ID, message: 'Cancelled.' })],
    });
    const harness = createFlowHarness(agentClient);

    await harness.sendText('action');
    const { ctx } = await harness.pressButton(`confirm:decline:${THREAD_ID}`);

    expect(ctx.answerCbQuery).toHaveBeenCalledWith('Declined.');
    expect(ctx.reply).toHaveBeenCalledWith('❌ Declined', { parse_mode: 'MarkdownV2' });
    expect(ctx.editMessageText).not.toHaveBeenCalled();
  });
});
