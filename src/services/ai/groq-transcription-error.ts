export type GroqTranscriptionErrorCategory =
  | 'rate_limit'
  | 'timeout'
  | 'connection'
  | 'server'
  | 'authentication'
  | 'permission'
  | 'invalid_audio'
  | 'payload_too_large'
  | 'cancelled'
  | 'unknown';

export interface GroqTranscriptionErrorOptions {
  category: GroqTranscriptionErrorCategory;
  message: string;
  retryable: boolean;
  status?: number;
  providerRequestId?: string;
  providerErrorType?: string;
  attempts: number;
  retryAfterSeconds?: number;
  cause?: unknown;
}

export class GroqTranscriptionError extends Error {
  readonly category: GroqTranscriptionErrorCategory;
  readonly retryable: boolean;
  readonly status?: number;
  readonly providerRequestId?: string;
  readonly providerErrorType?: string;
  readonly attempts: number;
  readonly retryAfterSeconds?: number;

  constructor(options: GroqTranscriptionErrorOptions) {
    super(options.message);
    if (options.cause !== undefined) {
      Object.defineProperty(this, 'cause', {
        value: options.cause,
        configurable: true,
      });
    }
    this.name = 'GroqTranscriptionError';
    this.category = options.category;
    this.retryable = options.retryable;
    this.status = options.status;
    this.providerRequestId = options.providerRequestId;
    this.providerErrorType = options.providerErrorType;
    this.attempts = options.attempts;
    this.retryAfterSeconds = options.retryAfterSeconds;
  }
}
