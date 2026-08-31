import { FileService } from '../../../../src/services/telegram/file.service';
import { AudioAdmissionError } from '../../../../src/utils/ai/audio-admission-error';
import { AUDIO_LIMITS } from '../../../../src/utils/ai/audio-limits';
import { logger } from '../../../../src/utils/logger';

describe('FileService', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    jest.restoreAllMocks();
  });

  // Streamed-download fetch stub: yields `chunks` then done, and records cancellation.
  function streamingFetch(chunks: number[][], contentLength: string | null = null) {
    const cancel = jest.fn().mockResolvedValue(undefined);
    let index = 0;
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      headers: { get: jest.fn().mockReturnValue(contentLength) },
      body: {
        getReader: () => ({
          read: jest.fn(async () =>
            index < chunks.length
              ? { done: false, value: Uint8Array.from(chunks[index++]) }
              : { done: true, value: undefined },
          ),
          cancel,
          releaseLock: jest.fn(),
        }),
      },
    }) as any;
    return { cancel };
  }

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

    await expect(service.getFileUrl('file-id')).rejects.toThrow('Telegram file is unavailable');
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

    await expect(service.downloadFile('private-id', 5)).rejects.toThrow(AudioAdmissionError);
    expect(reads).toBe(2);
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  describe('audio size admission', () => {
    function makeService(getFile: jest.Mock) {
      return new FileService('token-123', { getFile } as any);
    }

    it('accepts a file of exactly the 20 MiB limit', async () => {
      const service = makeService(
        jest.fn().mockResolvedValue({
          file_path: 'voice/file.ogg',
          file_size: 20 * 1024 * 1024,
        }),
      );

      await expect(service.getFileUrl('file-id', AUDIO_LIMITS.MAX_INPUT_BYTES)).resolves.toBe(
        'https://api.telegram.org/file/bottoken-123/voice/file.ogg',
      );
    });

    it('rejects a file one byte over the limit as too_large', async () => {
      const service = makeService(
        jest.fn().mockResolvedValue({
          file_path: 'voice/file.ogg',
          file_size: 20 * 1024 * 1024 + 1,
        }),
      );

      const error = await service
        .getFileUrl('file-id', AUDIO_LIMITS.MAX_INPUT_BYTES)
        .catch((e) => e);

      expect(error).toBeInstanceOf(AudioAdmissionError);
      expect(error.reason).toBe('too_large');
      expect(error.observed).toBe(20 * 1024 * 1024 + 1);
      expect(error.limit).toBe(AUDIO_LIMITS.MAX_INPUT_BYTES);
    });

    it('defers a missing declared size to the later download checks', async () => {
      const service = makeService(jest.fn().mockResolvedValue({ file_path: 'voice/file.ogg' }));

      await expect(service.getFileUrl('file-id', AUDIO_LIMITS.MAX_INPUT_BYTES)).resolves.toContain(
        'voice/file.ogg',
      );
    });

    it('behaves exactly as before when no limit is supplied', async () => {
      const service = makeService(
        jest.fn().mockResolvedValue({
          file_path: 'voice/huge.ogg',
          file_size: 500 * 1024 * 1024,
        }),
      );

      await expect(service.getFileUrl('file-id')).resolves.toBe(
        'https://api.telegram.org/file/bottoken-123/voice/huge.ogg',
      );
    });

    it.each([['400: Bad Request: file is too big'], ['400: Bad Request: FILE IS TOO BIG']])(
      "maps Telegram's own oversized refusal (%s) to a Jarvis admission error",
      async (message) => {
        const service = makeService(jest.fn().mockRejectedValue(new Error(message)));

        const error = await service
          .getFileUrl('file-id', AUDIO_LIMITS.MAX_INPUT_BYTES)
          .catch((e) => e);

        expect(error).toBeInstanceOf(AudioAdmissionError);
        expect(error.reason).toBe('too_large');
        expect(error.message).not.toContain('Telegram file is unavailable');
      },
    );

    it('still reports other getFile failures generically and logs no URL or token', async () => {
      const error = jest.spyOn(logger, 'error').mockImplementation();
      const service = makeService(jest.fn().mockRejectedValue(new Error('502: Bad Gateway')));

      await expect(
        service.getFileUrl('secret-file-id', AUDIO_LIMITS.MAX_INPUT_BYTES),
      ).rejects.toThrow('Telegram file is unavailable');

      expect(error).toHaveBeenCalledWith(
        'telegram.file.resolve_failed',
        expect.objectContaining({ oversized: false }),
      );
      const logged = JSON.stringify(error.mock.calls);
      expect(logged).not.toContain('token-123');
      expect(logged).not.toContain('api.telegram.org');
      expect(logged).not.toContain('secret-file-id');
    });

    it('marks an oversized resolve failure in the log line', async () => {
      const error = jest.spyOn(logger, 'error').mockImplementation();
      const service = makeService(
        jest.fn().mockResolvedValue({ file_path: 'voice/f.ogg', file_size: 99_000_000 }),
      );

      await expect(service.getFileUrl('file-id', AUDIO_LIMITS.MAX_INPUT_BYTES)).rejects.toThrow(
        AudioAdmissionError,
      );
      expect(error).toHaveBeenCalledWith(
        'telegram.file.resolve_failed',
        expect.objectContaining({ oversized: true }),
      );
    });

    it('forwards the byte limit from downloadFile to getFileUrl', async () => {
      const getFile = jest.fn().mockResolvedValue({
        file_path: 'voice/file.ogg',
        file_size: AUDIO_LIMITS.MAX_INPUT_BYTES + 1,
      });
      const service = makeService(getFile);
      global.fetch = jest.fn() as any;

      await expect(
        service.downloadFile('file-id', AUDIO_LIMITS.MAX_INPUT_BYTES),
      ).rejects.toBeInstanceOf(AudioAdmissionError);
      expect(global.fetch).not.toHaveBeenCalled();
    });

    it('rejects an oversized Content-Length before reading the body', async () => {
      const service = makeService(jest.fn().mockResolvedValue({ file_path: 'voice/file.ogg' }));
      const { cancel } = streamingFetch([[1, 2, 3]], String(AUDIO_LIMITS.MAX_INPUT_BYTES + 1));

      const error = await service
        .downloadFile('file-id', AUDIO_LIMITS.MAX_INPUT_BYTES)
        .catch((e) => e);

      expect(error).toBeInstanceOf(AudioAdmissionError);
      expect(error.reason).toBe('too_large');
      expect(cancel).not.toHaveBeenCalled();
    });

    it('cancels the stream as soon as the byte counter crosses the limit', async () => {
      const service = makeService(jest.fn().mockResolvedValue({ file_path: 'voice/file.ogg' }));
      const { cancel } = streamingFetch([
        [1, 2, 3],
        [4, 5, 6],
        [7, 8, 9],
      ]);

      await expect(service.downloadFile('file-id', 4)).rejects.toBeInstanceOf(AudioAdmissionError);
      expect(cancel).toHaveBeenCalledTimes(1);
    });

    it('returns the buffer when the stream stays inside the limit', async () => {
      const service = makeService(jest.fn().mockResolvedValue({ file_path: 'voice/file.ogg' }));
      streamingFetch([
        [1, 2],
        [3, 4],
      ]);

      await expect(service.downloadFile('file-id', 10)).resolves.toEqual(Buffer.from([1, 2, 3, 4]));
    });
  });
});
