import { EventEmitter } from 'events';

// Mock child_process before importing the module under test
jest.mock('child_process', () => ({
  spawn: jest.fn(),
}));

jest.mock('fs/promises', () => ({
  writeFile: jest.fn().mockResolvedValue(undefined),
  unlink: jest.fn().mockResolvedValue(undefined),
  readFile: jest.fn().mockResolvedValue(Buffer.from('converted-mp3-data')),
}));

jest.mock('@ffmpeg-installer/ffmpeg', () => ({
  path: '/usr/local/bin/ffmpeg',
}));

import { AudioConverter } from '../../../../src/utils/ai/audioConverter';
import { spawn } from 'child_process';
import { writeFile, unlink } from 'fs/promises';

const mockSpawn = spawn as jest.MockedFunction<typeof spawn>;
const mockWriteFile = writeFile as jest.MockedFunction<typeof writeFile>;
const mockUnlink = unlink as jest.MockedFunction<typeof unlink>;

interface MockProcess extends EventEmitter {
  stderr: EventEmitter;
  kill: jest.Mock;
}

function createMockProcess(): MockProcess {
  const proc = new EventEmitter() as MockProcess;
  proc.stderr = new EventEmitter();
  proc.kill = jest.fn();
  return proc;
}

describe('AudioConverter', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe('needsConversion()', () => {
    it('returns true for all convertible formats', () => {
      const convertible = ['oga', 'ogg', 'webm', 'opus', 'flac', 'aac', 'wma', 'amr'];
      for (const format of convertible) {
        expect(AudioConverter.needsConversion(format)).toBe(true);
      }
    });

    it('returns false for non-convertible formats', () => {
      const nonConvertible = ['mp3', 'wav', 'm4a', 'unknown'];
      for (const format of nonConvertible) {
        expect(AudioConverter.needsConversion(format)).toBe(false);
      }
    });

    it('is case-insensitive', () => {
      expect(AudioConverter.needsConversion('OGG')).toBe(true);
      expect(AudioConverter.needsConversion('Flac')).toBe(true);
      expect(AudioConverter.needsConversion('WMA')).toBe(true);
      expect(AudioConverter.needsConversion('OGA')).toBe(true);
      expect(AudioConverter.needsConversion('WEBM')).toBe(true);
    });
  });

  describe('isFFmpegAvailable()', () => {
    it('returns true when ffmpeg process exits with code 0', async () => {
      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      const promise = AudioConverter.isFFmpegAvailable();
      mockProc.emit('close', 0);

      const result = await promise;
      expect(result).toBe(true);
      expect(mockSpawn).toHaveBeenCalledWith('/usr/local/bin/ffmpeg', ['-version']);
    });

    it('returns false when ffmpeg process exits with non-zero code', async () => {
      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      const promise = AudioConverter.isFFmpegAvailable();
      mockProc.emit('close', 1);

      const result = await promise;
      expect(result).toBe(false);
    });

    it('returns false when spawn emits an error event', async () => {
      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      const promise = AudioConverter.isFFmpegAvailable();
      mockProc.emit('error', new Error('spawn ENOENT'));

      const result = await promise;
      expect(result).toBe(false);
    });

    it('returns false on timeout', async () => {
      jest.useFakeTimers();

      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      const promise = AudioConverter.isFFmpegAvailable();

      // Advance past the 5s timeout
      jest.advanceTimersByTime(5000);

      const result = await promise;
      expect(result).toBe(false);
      expect(mockProc.kill).toHaveBeenCalled();

      jest.useRealTimers();
    });
  });

  describe('convertToMp3()', () => {
    const inputBuffer = Buffer.from('fake-audio-data');
    const convertedData = Buffer.from('converted-mp3-data');

    beforeEach(() => {
      mockWriteFile.mockResolvedValue(undefined);
      mockUnlink.mockResolvedValue(undefined);
    });

    it('returns correct ConversionResult on successful conversion', async () => {
      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      // writeFile resolves, then we emit close on next tick so performConversion resolves
      mockWriteFile.mockImplementation(async () => {
        process.nextTick(() => mockProc.emit('close', 0));
      });

      const result = await AudioConverter.convertToMp3(inputBuffer, 'ogg', 123);

      expect(result.convertedBuffer).toEqual(convertedData);
      expect(result.originalFormat).toBe('ogg');
      expect(result.targetFormat).toBe('mp3');
      expect(result.originalSizeBytes).toBe(inputBuffer.length);
      expect(result.convertedSizeBytes).toBe(convertedData.length);
      expect(typeof result.conversionTimeMs).toBe('number');
      expect(result.conversionTimeMs).toBeGreaterThanOrEqual(0);
    });

    it('writes input buffer to a temp file before conversion', async () => {
      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      mockWriteFile.mockImplementation(async () => {
        process.nextTick(() => mockProc.emit('close', 0));
      });

      await AudioConverter.convertToMp3(inputBuffer, 'opus');

      expect(mockWriteFile).toHaveBeenCalledTimes(1);
      const writtenPath = (mockWriteFile.mock.calls[0] as any[])[0] as string;
      expect(writtenPath).toMatch(/input_.*\.opus$/);
      expect((mockWriteFile.mock.calls[0] as any[])[1]).toBe(inputBuffer);
    });

    it('cleans up both temp files on success', async () => {
      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      mockWriteFile.mockImplementation(async () => {
        process.nextTick(() => mockProc.emit('close', 0));
      });

      await AudioConverter.convertToMp3(inputBuffer, 'ogg');

      expect(mockUnlink).toHaveBeenCalledTimes(2);
      const unlinkPaths = mockUnlink.mock.calls.map((call) => (call as any[])[0] as string);
      expect(unlinkPaths[0]).toMatch(/input_.*\.ogg$/);
      expect(unlinkPaths[1]).toMatch(/output_.*\.mp3$/);
    });

    it('cleans up temp files even on conversion failure', async () => {
      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      mockWriteFile.mockImplementation(async () => {
        process.nextTick(() => {
          mockProc.stderr.emit('data', Buffer.from('Invalid data found when processing input'));
          mockProc.emit('close', 1);
        });
      });

      await expect(AudioConverter.convertToMp3(inputBuffer, 'ogg')).rejects.toThrow();

      expect(mockUnlink).toHaveBeenCalledTimes(2);
    });

    it('throws "FFmpeg executable not found" when spawn emits ENOENT error', async () => {
      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      mockWriteFile.mockImplementation(async () => {
        process.nextTick(() => {
          mockProc.emit('error', new Error('spawn /usr/local/bin/ffmpeg ENOENT'));
        });
      });

      await expect(
        AudioConverter.convertToMp3(inputBuffer, 'ogg'),
      ).rejects.toThrow(
        'Audio conversion failed: FFmpeg executable not found. Ensure `@ffmpeg-installer/ffmpeg` is installed.',
      );
    });

    it('throws on non-zero exit code with FFmpeg error details', async () => {
      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      mockWriteFile.mockImplementation(async () => {
        process.nextTick(() => {
          mockProc.stderr.emit('data', Buffer.from('Invalid data found when processing input\n'));
          mockProc.emit('close', 1);
        });
      });

      await expect(
        AudioConverter.convertToMp3(inputBuffer, 'ogg'),
      ).rejects.toThrow('Audio conversion failed: FFmpeg conversion failed:');
    });

    it('kills process and throws timeout error after CONVERSION_TIMEOUT_MS', async () => {
      jest.useFakeTimers();

      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      mockWriteFile.mockImplementation(() => {
        return Promise.resolve();
      });

      const promise = AudioConverter.convertToMp3(inputBuffer, 'ogg');

      // Allow the writeFile microtask to resolve and spawn to be called
      await jest.advanceTimersByTimeAsync(0);

      // Advance past the 30s conversion timeout
      jest.advanceTimersByTime(30000);

      // Emit close to clear the second 'close' listener (timeout clears on close)
      mockProc.emit('close', null);

      await expect(promise).rejects.toThrow('Audio conversion timed out after 30 seconds');
      expect(mockProc.kill).toHaveBeenCalledWith('SIGKILL');

      // Cleanup still happens
      expect(mockUnlink).toHaveBeenCalledTimes(2);

      jest.useRealTimers();
    });

    it('spawns ffmpeg with correct arguments', async () => {
      const mockProc = createMockProcess();
      mockSpawn.mockReturnValue(mockProc as any);

      mockWriteFile.mockImplementation(async () => {
        process.nextTick(() => mockProc.emit('close', 0));
      });

      await AudioConverter.convertToMp3(inputBuffer, 'flac');

      expect(mockSpawn).toHaveBeenCalledWith(
        '/usr/local/bin/ffmpeg',
        expect.arrayContaining([
          '-i',
          '-acodec',
          'libmp3lame',
          '-ab',
          '128k',
          '-ar',
          '44100',
          '-ac',
          '2',
          '-f',
          'mp3',
          '-y',
        ]),
      );
    });
  });

  describe('getSupportedFormats()', () => {
    it('returns the full list of convertible formats', () => {
      const formats = AudioConverter.getSupportedFormats();
      expect(formats).toEqual(['oga', 'ogg', 'webm', 'opus', 'flac', 'aac', 'wma', 'amr']);
    });

    it('returns all 8 supported formats', () => {
      const formats = AudioConverter.getSupportedFormats();
      expect(formats).toHaveLength(8);
    });
  });

  describe('getTargetFormat()', () => {
    it('returns mp3', () => {
      expect(AudioConverter.getTargetFormat()).toBe('mp3');
    });
  });
});
