import { FileService } from '../../../../src/services/telegram/file.service';

describe('FileService', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it('detects supported audio mime types', () => {
    const telegram = { getFile: jest.fn() } as any;
    const service = new FileService('token-123', telegram);

    expect(service.isAudioFile('audio/flac')).toBe(true);
    expect(service.isAudioFile('audio/mpeg')).toBe(true);
    expect(service.isAudioFile('audio/mp4')).toBe(true);
    expect(service.isAudioFile('audio/m4a')).toBe(true);
    expect(service.isAudioFile('audio/ogg')).toBe(true);
    expect(service.isAudioFile('audio/webm')).toBe(true);
    expect(service.isAudioFile('audio/wav')).toBe(true);
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
      'Telegram file is unavailable',
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

    await expect(service.downloadFile('file-id')).rejects.toThrow('Telegram file download failed');
  });

  it('stops a streaming download as soon as its byte allowance is exceeded', async () => {
    const telegram = {
      getFile: jest.fn().mockResolvedValue({ file_path: 'photos/file.jpg' }),
    } as any;
    const cancel = jest.fn().mockResolvedValue(undefined);
    let reads = 0;
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      headers: { get: jest.fn().mockReturnValue(null) },
      body: {
        getReader: () => ({
          read: jest.fn(async () => {
            reads += 1;
            return reads === 1
              ? { done: false, value: Uint8Array.from([1, 2, 3]) }
              : { done: false, value: Uint8Array.from([4, 5, 6]) };
          }),
          cancel,
          releaseLock: jest.fn(),
        }),
      },
    }) as any;
    const service = new FileService('token-123', telegram);

    await expect(service.downloadFile('private-id', 5)).rejects.toThrow(
      'Telegram file exceeds byte limit',
    );
    expect(reads).toBe(2);
    expect(cancel).toHaveBeenCalledTimes(1);
  });
});
