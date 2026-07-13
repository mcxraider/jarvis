export type ErrorCategory = 'user_actionable' | 'transient' | 'permanent';

export interface ClassifiedError {
  category: ErrorCategory;
  userMessage: string;
  shouldLog: 'warn' | 'error';
}

interface ErrorRule {
  match: (msg: string) => boolean;
  category: ErrorCategory;
  userMessage: string;
  shouldLog: 'warn' | 'error';
}

const ERROR_RULES: ErrorRule[] = [
  // User-actionable: the user can fix the problem by changing their input
  {
    match: (msg) => msg.includes('Message cannot be empty'),
    category: 'user_actionable',
    userMessage: 'Please send a message with some text.',
    shouldLog: 'warn',
  },
  {
    match: (msg) => msg.includes('exceeds maximum allowed length'),
    category: 'user_actionable',
    userMessage: 'Message too long. Please keep it under 4000 characters.',
    shouldLog: 'warn',
  },
  {
    match: (msg) => msg.includes('File size') && msg.includes('exceeds'),
    category: 'user_actionable',
    userMessage: 'Audio file is too large. Maximum size is 25 MB.',
    shouldLog: 'warn',
  },
  {
    match: (msg) => msg.includes('Unsupported audio format'),
    category: 'user_actionable',
    userMessage: 'Unsupported audio format. Please send MP3, OGG, WAV, or M4A.',
    shouldLog: 'warn',
  },
  {
    match: (msg) => msg.includes('Audio format conversion is not available'),
    category: 'user_actionable',
    userMessage:
      'This format requires conversion but the converter is unavailable. Please send MP3 or WAV.',
    shouldLog: 'warn',
  },
  {
    match: (msg) => msg.includes('Audio format conversion failed'),
    category: 'user_actionable',
    userMessage: 'Audio format conversion failed. Please send MP3 or WAV directly.',
    shouldLog: 'warn',
  },

  // Transient: likely resolves on retry
  {
    match: (msg) => msg.includes('Service is temporarily busy'),
    category: 'transient',
    userMessage: 'Service is busy. Please try again in a moment.',
    shouldLog: 'warn',
  },
  {
    match: (msg) => msg.includes('temporarily unavailable'),
    category: 'transient',
    userMessage: 'Jarvis is temporarily unavailable. Please try again in a moment.',
    shouldLog: 'warn',
  },
  {
    match: (msg) => msg.includes('Failed to download'),
    category: 'transient',
    userMessage: 'Could not download the file. Please try sending it again.',
    shouldLog: 'warn',
  },
  {
    match: (msg) => msg.includes('The operation was aborted'),
    category: 'transient',
    userMessage: 'Request timed out. Please try again.',
    shouldLog: 'warn',
  },
  {
    match: (msg) => /API returned 5\d\d/.test(msg),
    category: 'transient',
    userMessage: 'Jarvis backend is temporarily unavailable. Please try again in a moment.',
    shouldLog: 'warn',
  },
];

const DEFAULT_CLASSIFIED: ClassifiedError = {
  category: 'permanent',
  userMessage: 'Something went wrong processing your request. Please try again.',
  shouldLog: 'error',
};

export function classifyError(error: Error): ClassifiedError {
  if (error instanceof GroqTranscriptionError) {
    switch (error.category) {
      case 'rate_limit': {
        const retryGuidance =
          error.retryAfterSeconds && error.retryAfterSeconds <= 60
            ? ` Please try again in about ${Math.ceil(error.retryAfterSeconds)} seconds.`
            : ' Please try again shortly.';
        return {
          category: 'transient',
          userMessage: `Voice transcription is temporarily rate-limited.${retryGuidance}`,
          shouldLog: 'warn',
        };
      }
      case 'timeout':
      case 'connection':
      case 'server':
        return {
          category: 'transient',
          userMessage: 'Voice transcription is temporarily unavailable. Please try again shortly.',
          shouldLog: 'warn',
        };
      case 'payload_too_large':
        return {
          category: 'user_actionable',
          userMessage: 'Audio file is too large. Maximum size is 25 MB.',
          shouldLog: 'warn',
        };
      case 'invalid_audio':
        return {
          category: 'user_actionable',
          userMessage: 'Unsupported audio format. Please send MP3, OGG, WAV, or M4A.',
          shouldLog: 'warn',
        };
      case 'authentication':
      case 'permission':
        return {
          category: 'permanent',
          userMessage:
            'Voice transcription is currently unavailable. The service has been notified.',
          shouldLog: 'error',
        };
      default:
        break;
    }
  }

  const msg = error.message;

  for (const rule of ERROR_RULES) {
    if (rule.match(msg)) {
      return { category: rule.category, userMessage: rule.userMessage, shouldLog: rule.shouldLog };
    }
  }

  return DEFAULT_CLASSIFIED;
}
import { GroqTranscriptionError } from '../../ai/groq-transcription-error';
