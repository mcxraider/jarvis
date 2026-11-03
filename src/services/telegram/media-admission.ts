import { positiveInteger } from '../database/runtime-pool';

/** Reject before expensive work; callers preserve their durable input for retry. */
export class MediaAdmission {
  private active = 0;
  constructor(private readonly limit: number) {}
  async run<T>(work: () => Promise<T>): Promise<T> {
    if (this.active >= this.limit)
      throw new Error('Jarvis media processing is busy. Please retry in a few seconds.');
    this.active++;
    try {
      return await work();
    } finally {
      this.active--;
    }
  }
}
let images: MediaAdmission | undefined;
export function imageAdmission(): MediaAdmission {
  return (images ??= new MediaAdmission(positiveInteger('JARVIS_MAX_CONCURRENT_IMAGE_JOBS', 1)));
}
