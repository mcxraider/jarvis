import { classifyError } from '../../../../../src/services/telegram/errors/classified-error';
import { GroqTranscriptionError } from '../../../../../src/services/ai/groq-transcription-error';
import { AudioAdmissionError } from '../../../../../src/utils/ai/audio-admission-error';
import { AUDIO_LIMIT_MESSAGES } from '../../../../../src/utils/ai/audio-limits';

describe('classifyError', () => {
  describe('audio admission errors', () => {
    it('classifies an oversized file with the shared 20 MB copy', () => {
      const result = classifyError(new AudioAdmissionError('too_large', { observed: 1, limit: 0 }));

      expect(result).toEqual({
        category: 'user_actionable',
        userMessage: AUDIO_LIMIT_MESSAGES.tooLarge,
        shouldLog: 'warn',
      });
      // Pin the exact user-facing wording, not just the constant reference.
      expect(result.userMessage).toBe(
        'That audio file is too large. Jarvis can only accept files up to 20 MB.',
      );
    });

    it('classifies over-long audio with the shared 20-minute copy', () => {
      const result = classifyError(new AudioAdmissionError('too_long', { observed: 3000 }));

      expect(result).toEqual({
        category: 'user_actionable',
        userMessage: AUDIO_LIMIT_MESSAGES.tooLong,
        shouldLog: 'warn',
      });
      expect(result.userMessage).toBe(
        'That audio is too long. Please send audio that is 20 minutes or shorter.',
      );
    });

    it('wins over any message-pattern rule', () => {
      // This message would otherwise match the max-length rule.
      const error = new AudioAdmissionError('too_long');
      Object.defineProperty(error, 'message', {
        value: 'File size 30MB exceeds maximum allowed length',
      });

      expect(classifyError(error).userMessage).toBe(AUDIO_LIMIT_MESSAGES.tooLong);
    });

    it('no longer mentions 25 MB for size rules', () => {
      const cases = [
        classifyError(new AudioAdmissionError('too_large')),
        classifyError(new Error('File size 30MB exceeds the upload limit')),
        classifyError(
          new GroqTranscriptionError({
            category: 'payload_too_large',
            message: 'payload too large',
            retryable: false,
            status: 413,
            attempts: 1,
          }),
        ),
      ];

      for (const result of cases) {
        expect(result.userMessage).toBe(AUDIO_LIMIT_MESSAGES.tooLarge);
        expect(result.userMessage).not.toContain('25 MB');
        expect(result.category).toBe('user_actionable');
      }
    });
  });

  describe('typed Groq transcription errors', () => {
    it('returns rate-limit retry guidance', () => {
      const result = classifyError(
        new GroqTranscriptionError({
          category: 'rate_limit',
          message: 'rate limited',
          retryable: true,
          status: 429,
          attempts: 2,
          retryAfterSeconds: 4,
        }),
      );

      expect(result).toEqual({
        category: 'transient',
        userMessage:
          'Voice transcription is temporarily rate-limited. Please try again in about 4 seconds.',
        shouldLog: 'warn',
      });
    });

    it('keeps authentication details out of the user response', () => {
      const result = classifyError(
        new GroqTranscriptionError({
          category: 'authentication',
          message: 'invalid secret key',
          retryable: false,
          status: 401,
          attempts: 1,
        }),
      );

      expect(result).toEqual({
        category: 'permanent',
        userMessage: 'Voice transcription is currently unavailable. The service has been notified.',
        shouldLog: 'error',
      });
    });
  });

  describe('user_actionable errors', () => {
    it('classifies empty message error', () => {
      const result = classifyError(new Error('Message cannot be empty'));

      expect(result).toEqual({
        category: 'user_actionable',
        userMessage: 'Please send a message with some text.',
        shouldLog: 'warn',
      });
    });

    it('classifies message exceeding maximum length', () => {
      const result = classifyError(
        new Error('Input exceeds maximum allowed length of 4000 characters'),
      );

      expect(result).toEqual({
        category: 'user_actionable',
        userMessage: 'Message too long. Please keep it under 4000 characters.',
        shouldLog: 'warn',
      });
    });

    it('classifies file size exceeds limit', () => {
      const result = classifyError(new Error('File size 30MB exceeds the allowed limit'));

      expect(result).toEqual({
        category: 'user_actionable',
        userMessage: AUDIO_LIMIT_MESSAGES.tooLarge,
        shouldLog: 'warn',
      });
    });

    it('classifies unsupported audio format', () => {
      const result = classifyError(new Error('Unsupported audio format: FLAC'));

      expect(result).toEqual({
        category: 'user_actionable',
        userMessage: 'Unsupported audio format. Please send MP3, OGG, WAV, or M4A.',
        shouldLog: 'warn',
      });
    });

    it('classifies audio conversion unavailable', () => {
      const result = classifyError(
        new Error('Audio format conversion is not available on this system'),
      );

      expect(result).toEqual({
        category: 'user_actionable',
        userMessage:
          'This format requires conversion but the converter is unavailable. Please send MP3 or WAV.',
        shouldLog: 'warn',
      });
    });

    it('classifies audio conversion failure', () => {
      const result = classifyError(new Error('Audio format conversion failed for file voice.ogg'));

      expect(result).toEqual({
        category: 'user_actionable',
        userMessage: 'Audio format conversion failed. Please send MP3 or WAV directly.',
        shouldLog: 'warn',
      });
    });
  });

  describe('transient errors', () => {
    it('classifies service temporarily busy', () => {
      const result = classifyError(new Error('Service is temporarily busy, try again later'));

      expect(result).toEqual({
        category: 'transient',
        userMessage: 'Service is busy. Please try again in a moment.',
        shouldLog: 'warn',
      });
    });

    it('classifies temporarily unavailable', () => {
      const result = classifyError(new Error('The backend is temporarily unavailable'));

      expect(result).toEqual({
        category: 'transient',
        userMessage: 'Jarvis is temporarily unavailable. Please try again in a moment.',
        shouldLog: 'warn',
      });
    });

    it('classifies download failure', () => {
      const result = classifyError(new Error('Failed to download file from Telegram servers'));

      expect(result).toEqual({
        category: 'transient',
        userMessage: 'Could not download the file. Please try sending it again.',
        shouldLog: 'warn',
      });
    });

    it('classifies operation aborted / timeout', () => {
      const result = classifyError(new Error('The operation was aborted due to timeout'));

      expect(result).toEqual({
        category: 'transient',
        userMessage: 'Request timed out. Please try again.',
        shouldLog: 'warn',
      });
    });

    it('classifies 5xx API errors', () => {
      const result = classifyError(new Error('API returned 503 Service Unavailable'));

      expect(result).toEqual({
        category: 'transient',
        userMessage: 'Jarvis backend is temporarily unavailable. Please try again in a moment.',
        shouldLog: 'warn',
      });
    });

    it('matches API returned 500', () => {
      const result = classifyError(new Error('API returned 500 Internal Server Error'));
      expect(result.category).toBe('transient');
    });

    it('matches API returned 502', () => {
      const result = classifyError(new Error('API returned 502 Bad Gateway'));
      expect(result.category).toBe('transient');
    });
  });

  describe('permanent (default) errors', () => {
    it('returns default classification for unrecognized errors', () => {
      const result = classifyError(new Error('Something completely unexpected happened'));

      expect(result).toEqual({
        category: 'permanent',
        userMessage: 'Something went wrong processing your request. Please try again.',
        shouldLog: 'error',
      });
    });

    it('returns default classification for empty error message', () => {
      const result = classifyError(new Error(''));

      expect(result).toEqual({
        category: 'permanent',
        userMessage: 'Something went wrong processing your request. Please try again.',
        shouldLog: 'error',
      });
    });

    it('returns default classification for a generic network error', () => {
      const result = classifyError(new Error('ECONNREFUSED 127.0.0.1:8000'));

      expect(result).toEqual({
        category: 'permanent',
        userMessage: 'Something went wrong processing your request. Please try again.',
        shouldLog: 'error',
      });
    });
  });

  describe('edge cases', () => {
    describe('rule ordering (first match wins)', () => {
      it('matches max-length rule over file-size rule when message contains "exceeds maximum allowed length"', () => {
        // This message contains patterns for both the max-length rule (index 1) and
        // the file-size rule (index 2). The max-length rule appears first in
        // ERROR_RULES, so it wins.
        const result = classifyError(new Error('File size 30MB exceeds maximum allowed length'));

        expect(result).toEqual({
          category: 'user_actionable',
          userMessage: 'Message too long. Please keep it under 4000 characters.',
          shouldLog: 'warn',
        });
      });

      it('matches file-size rule when message has "File size" and "exceeds" but NOT "maximum allowed length"', () => {
        // Only the file-size rule matches here, verifying its pattern independently.
        const result = classifyError(new Error('File size 30MB exceeds the upload limit'));

        expect(result).toEqual({
          category: 'user_actionable',
          userMessage: AUDIO_LIMIT_MESSAGES.tooLarge,
          shouldLog: 'warn',
        });
      });
    });

    describe('5xx regex matching', () => {
      it('matches 500', () => {
        const result = classifyError(new Error('API returned 500'));
        expect(result.category).toBe('transient');
      });

      it('matches 502', () => {
        const result = classifyError(new Error('API returned 502'));
        expect(result.category).toBe('transient');
      });

      it('matches 503', () => {
        const result = classifyError(new Error('API returned 503'));
        expect(result.category).toBe('transient');
      });

      it('matches 599', () => {
        const result = classifyError(new Error('API returned 599'));
        expect(result.category).toBe('transient');
      });

      it('does NOT match 400', () => {
        const result = classifyError(new Error('API returned 400 Bad Request'));
        expect(result.category).toBe('permanent');
      });

      it('does NOT match 404', () => {
        const result = classifyError(new Error('API returned 404 Not Found'));
        expect(result.category).toBe('permanent');
      });

      it('does NOT match 429', () => {
        const result = classifyError(new Error('API returned 429 Too Many Requests'));
        expect(result.category).toBe('permanent');
      });
    });

    describe('partial matches work within longer messages', () => {
      it('matches empty message rule embedded in longer text', () => {
        const result = classifyError(
          new Error('Validation failed: Message cannot be empty (field: body)'),
        );
        expect(result.category).toBe('user_actionable');
        expect(result.userMessage).toBe('Please send a message with some text.');
      });

      it('matches temporarily unavailable embedded in longer text', () => {
        const result = classifyError(
          new Error(
            'LangGraph agent at http://localhost:8000 is temporarily unavailable (connection refused)',
          ),
        );
        expect(result.category).toBe('transient');
        expect(result.userMessage).toBe(
          'Jarvis is temporarily unavailable. Please try again in a moment.',
        );
      });

      it('matches download failure with full URL context', () => {
        const result = classifyError(
          new Error(
            'Failed to download https://api.telegram.org/file/bot123/voice.ogg: 403 Forbidden',
          ),
        );
        expect(result.category).toBe('transient');
        expect(result.userMessage).toBe(
          'Could not download the file. Please try sending it again.',
        );
      });

      it('matches 5xx regex with surrounding context', () => {
        const result = classifyError(
          new Error('Request to /invoke failed: API returned 502 after 3 retries'),
        );
        expect(result.category).toBe('transient');
      });
    });
  });
});
