jest.mock('../src/utils/logger', () => ({
  createRequestId: jest.fn((prefix = 'req') => `${prefix}_test`),
  truncateForLog: jest.fn((value?: string, maxLength = 80) =>
    value && value.length > maxLength ? `${value.slice(0, maxLength)}...` : value,
  ),
  logger: {
    info: jest.fn(),
    warn: jest.fn(),
    error: jest.fn(),
    debug: jest.fn(),
  },
}));
