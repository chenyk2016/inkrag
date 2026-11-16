/** 同一页面共享两个下载槽；同一客户端同时请求同一资产时共用下载。 */
type PendingAsset = {
  controller: AbortController;
  promise: Promise<Blob>;
  readers: number;
};

const MAX_ACTIVE = 2;
const MAX_WAITING = 64;
let active = 0;
const waiting: (() => void)[] = [];

/** 页面切换可以从等待队列移除请求，槽位在读取完整响应后归还。 */
async function inDownloadSlot<T>(
  signal: AbortSignal,
  work: () => Promise<T>,
): Promise<T> {
  signal.throwIfAborted();
  if (active < MAX_ACTIVE) {
    active += 1;
  } else {
    if (waiting.length >= MAX_WAITING)
      throw new Error("等待下载的原件过多，请稍后重试。");
    await new Promise<void>((resolve, reject) => {
      const admit = () => {
        signal.removeEventListener("abort", cancel);
        resolve();
      };
      const cancel = () => {
        const index = waiting.indexOf(admit);
        if (index >= 0) waiting.splice(index, 1);
        reject(signal.reason);
      };
      waiting.push(admit);
      signal.addEventListener("abort", cancel, { once: true });
    });
  }
  try {
    signal.throwIfAborted();
    return await work();
  } finally {
    const next = waiting.shift();
    if (next) next();
    else active -= 1;
  }
}

/** 可撤销退避。暂时繁忙时先归还槽位，其他可见资源可继续。 */
function wait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", cancel);
      resolve();
    }, ms);
    const cancel = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    signal.addEventListener("abort", cancel, { once: true });
  });
}

/** 每个 API 客户端只共享请求中的资源，不缓存用户资料。 */
export function createAssetReader(
  load: (id: string, signal: AbortSignal) => Promise<Blob>,
  retryDelay: (error: unknown) => number | null,
): (id: string, signal?: AbortSignal) => Promise<Blob> {
  const pending = new Map<string, PendingAsset>();

  /** 仅服务明确暂时繁忙才重试，读取超时、授权失败及文件损坏直接返回。 */
  async function download(id: string, signal: AbortSignal): Promise<Blob> {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await inDownloadSlot(signal, () =>
          load(id, AbortSignal.any([signal, AbortSignal.timeout(30000)])),
        );
      } catch (error) {
        const delay = retryDelay(error);
        if (signal.aborted || delay === null || attempt >= 2) throw error;
        await wait(delay + Math.random() * 200, signal);
      }
    }
  }

  return async (id, signal) => {
    signal?.throwIfAborted();
    let entry = pending.get(id);
    if (!entry) {
      const controller = new AbortController();
      entry = {
        controller,
        readers: 0,
        promise: download(id, controller.signal),
      };
      pending.set(id, entry);
    }
    const shared = entry;
    shared.readers += 1;
    return await new Promise<Blob>((resolve, reject) => {
      let done = false;
      const release = () => {
        if (done) return false;
        done = true;
        signal?.removeEventListener("abort", cancel);
        shared.readers -= 1;
        if (!shared.readers) {
          if (pending.get(id) === shared) pending.delete(id);
          shared.controller.abort();
        }
        return true;
      };
      const cancel = () => {
        if (release()) reject(signal?.reason);
      };
      signal?.addEventListener("abort", cancel, { once: true });
      void shared.promise.then(
        (blob) => {
          if (release()) resolve(blob);
        },
        (error: unknown) => {
          if (release()) reject(error);
        },
      );
    });
  };
}
