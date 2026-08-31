import {
  AgentImageBatchesSchema,
  AgentImagesSchema,
  MAX_AGENT_IMAGE_BYTES,
} from '../../../src/types/agent.types';

describe('AgentImagesSchema', () => {
  const image = (base64 = '/9j/2Q==') => ({
    image_url: `data:image/jpeg;base64,${base64}`,
    detail: 'auto',
  });

  it('accepts one to ten strict JPEG Base64 image records', () => {
    expect(AgentImagesSchema.safeParse([image()]).success).toBe(true);
    expect(AgentImagesSchema.safeParse(Array.from({ length: 10 }, () => image())).success).toBe(
      true,
    );
  });

  it('rejects malformed image input', () => {
    const invalid = [
      [],
      Array.from({ length: 11 }, () => image()),
      [{ image_url: 'data:image/png;base64,/9j/2Q==', detail: 'auto' }],
      [image('not-base64')],
      [{ ...image(), detail: 'original' }],
      [{ ...image(), extra: true }],
    ];
    expect(invalid.every((value) => !AgentImagesSchema.safeParse(value).success)).toBe(true);
  });

  it('rejects decoded image bytes above the aggregate limit', () => {
    const encoded = Buffer.alloc(MAX_AGENT_IMAGE_BYTES + 1).toString('base64');
    expect(AgentImagesSchema.safeParse([image(encoded)]).success).toBe(false);
  });
});

describe('AgentImageBatchesSchema', () => {
  const image = (bytes = Buffer.from([0xff, 0xd8, 0xff, 0xd9])) => ({
    image_url: `data:image/jpeg;base64,${bytes.toString('base64')}`,
    detail: 'auto',
  });

  it('accepts empty text turns and enforces cumulative image limits', () => {
    expect(AgentImageBatchesSchema.safeParse([[image()], [], [image()]]).success).toBe(true);
    expect(
      AgentImageBatchesSchema.safeParse([
        Array.from({ length: 6 }, () => image()),
        Array.from({ length: 5 }, () => image()),
      ]).success,
    ).toBe(false);
    const overHalf = Buffer.alloc(Math.floor(MAX_AGENT_IMAGE_BYTES / 2) + 1);
    expect(AgentImageBatchesSchema.safeParse([[image(overHalf)], [image(overHalf)]]).success).toBe(
      false,
    );
  });
});
