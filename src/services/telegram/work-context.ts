import { AsyncLocalStorage } from 'async_hooks';

export interface DispatchRecord {
  route: 'invoke' | 'resume';
  userId: string;
  requestId: string;
  threadId?: string;
}
export interface WorkContext {
  beforeDispatch(record: DispatchRecord): Promise<void>;
  assertOwned(): void;
  trackHandler?(task: Promise<unknown>): void;
  markAmbiguous?(): void;
}
export const telegramWorkContext = new AsyncLocalStorage<WorkContext>();
