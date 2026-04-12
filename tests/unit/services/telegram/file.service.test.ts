import { FileService } from '../../../../src/services/telegram/file.service';

describe('FileService', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it('detects supported audio mime types', () => {
    const telegram = { getFile: jest.fn() } as any;
    const service = new FileService('token-123', telegram);

    expect(service.isAudioFile('audio/mpeg')).toBe(true);
    expect(service.isAudioFile('application/pdf')).toBe(false);
    expect(service.isAudioFile(undefined)).toBe(false);
  });

  it('builds the Telegram download URL from file metadata', async () => {
    const telegram = {
      getFile: jest.fn().mockResolvedValue({ file_path: 'voice/file.ogg' }),
    } as any;
    const service = new FileService('token-123', telegram);

    await expect(service.getFileUrl('file-id')).resolves.toBe(
      'https://api.telegram.org/file/bottoken-123/voice/file.ogg',
    );
  });

  it('rejects when Telegram does not return a file path', async () => {
    const telegram = {
      getFile: jest.fn().mockResolvedValue({}),
    } as any;
    const service = new FileService('token-123', telegram);

    await expect(service.getFileUrl('file-id')).rejects.toThrow(
      'Failed to get file URL: File path not available',
    );
  });

  it('downloads a file buffer from Telegram', async () => {
    const telegram = {
      getFile: jest.fn().mockResolvedValue({ file_path: 'voice/file.ogg' }),
    } as any;
    const service = new FileService('token-123', telegram);
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      arrayBuffer: jest.fn().mockResolvedValue(Uint8Array.from([1, 2, 3]).buffer),
    }) as any;

    await expect(service.downloadFile('file-id')).resolves.toEqual(Buffer.from([1, 2, 3]));
  });

  it('surfaces HTTP failures when downloading a file', async () => {
    const telegram = {
      getFile: jest.fn().mockResolvedValue({ file_path: 'voice/file.ogg' }),
    } as any;
    const service = new FileService('token-123', telegram);
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
    }) as any;

    await expect(service.downloadFile('file-id')).rejects.toThrow('HTTP 502: Bad Gateway');
  });
});
