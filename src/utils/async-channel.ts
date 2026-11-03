/** Bounded single-producer channel; close wakes blocked readers AND writers. */
export class AsyncChunkQueue<T> {
  private buffer: T[] = [];
  private readers: Array<(value: T | null) => void> = [];
  private writers: Array<() => void> = [];
  private closed = false;
  constructor(private readonly capacity = 2) {}

  async push(value: T): Promise<void> {
    while (!this.closed && this.buffer.length >= this.capacity) {
      await new Promise<void>((resolve) => this.writers.push(resolve));
    }
    if (this.closed) throw new Error('Channel closed');
    const reader = this.readers.shift();
    if (reader) reader(value);
    else this.buffer.push(value);
  }
  async pull(): Promise<T | null> {
    const value = this.buffer.shift();
    if (value !== undefined) {
      this.writers.shift()?.();
      return value;
    }
    if (this.closed) return null;
    return new Promise((resolve) => this.readers.push(resolve));
  }
  close(): void {
    this.closed = true;
    this.readers.splice(0).forEach((resolve) => resolve(null));
    this.writers.splice(0).forEach((resolve) => resolve());
  }
}
