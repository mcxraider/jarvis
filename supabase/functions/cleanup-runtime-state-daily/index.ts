type CleanupResult = {
  cutoff_at: string;
  idempotency_results_deleted: number;
  checkpoint_writes_deleted: number;
  checkpoints_deleted: number;
  checkpoint_blobs_deleted: number;
  thread_messages_deleted: number;
};

type ExpiredThreadImage = { object_path: string };

const THREAD_IMAGE_BUCKET = "thread-images";
const STORAGE_DELETE_BATCH_SIZE = 100;
const UPSTREAM_TIMEOUT_MS = 3_000;

async function supabaseFetch(
  input: string,
  init: RequestInit,
): Promise<Response | null> {
  const controller = new AbortController();
  let timeout: number | undefined;
  try {
    return await Promise.race([
      fetch(input, { ...init, signal: controller.signal }),
      new Promise<null>((resolve) => {
        timeout = setTimeout(() => {
          controller.abort();
          resolve(null);
        }, UPSTREAM_TIMEOUT_MS);
      }),
    ]);
  } catch {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

async function errorCode(response: Response): Promise<string | undefined> {
  try {
    return ((await response.json()) as { code?: string }).code;
  } catch {
    return undefined;
  }
}

Deno.serve(async (request: Request) => {
  if (request.method !== "POST") {
    return Response.json({ error: "method_not_allowed" }, { status: 405 });
  }

  const cronSecret = request.headers.get("x-cron-secret");
  if (!cronSecret) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceRoleKey) {
    return Response.json({ error: "server_misconfigured" }, { status: 500 });
  }

  const apiUrl = supabaseUrl.replace(/\/+$/, "");
  const serviceHeaders = {
    apikey: serviceRoleKey,
    Authorization: `Bearer ${serviceRoleKey}`,
    "Content-Type": "application/json",
  };

  // The database helper authenticates the cron secret before any Storage mutation
  // and returns only expired object paths. It is installed with the memory tables.
  const imageListResponse = await supabaseFetch(
    `${apiUrl}/rest/v1/rpc/prepare_thread_memory_cleanup`,
    {
      method: "POST",
      headers: serviceHeaders,
      body: JSON.stringify({ p_cron_secret: cronSecret }),
    },
  );
  if (!imageListResponse) {
    return Response.json({ error: "cleanup_failed" }, { status: 500 });
  }
  if (!imageListResponse.ok) {
    const unauthorized = (await errorCode(imageListResponse)) === "42501";
    return Response.json(
      { error: unauthorized ? "unauthorized" : "cleanup_failed" },
      { status: unauthorized ? 401 : 500 },
    );
  }

  const expiredImages = await imageListResponse.json();
  if (!Array.isArray(expiredImages)) {
    return Response.json({ error: "cleanup_failed" }, { status: 500 });
  }

  const objectPaths = [
    ...new Set(
      (expiredImages as ExpiredThreadImage[])
        .map(({ object_path }) => object_path)
        .filter(Boolean),
    ),
  ];
  let threadImages = 0;
  for (let index = 0; index < objectPaths.length; index += STORAGE_DELETE_BATCH_SIZE) {
    const batch = objectPaths.slice(index, index + STORAGE_DELETE_BATCH_SIZE);
    const removeResponse = await supabaseFetch(
      `${apiUrl}/storage/v1/object/${THREAD_IMAGE_BUCKET}`,
      {
        method: "DELETE",
        headers: serviceHeaders,
        body: JSON.stringify({ prefixes: batch }),
      },
    );
    if (!removeResponse?.ok) {
      return Response.json({ error: "cleanup_failed" }, { status: 500 });
    }
    const removed = await removeResponse.json();
    threadImages += Array.isArray(removed) ? removed.length : 0;
  }

  const cleanupResponse = await supabaseFetch(
    `${apiUrl}/rest/v1/rpc/cleanup_runtime_state_daily`,
    {
      method: "POST",
      headers: serviceHeaders,
      body: JSON.stringify({ p_cron_secret: cronSecret }),
    },
  );
  if (!cleanupResponse) {
    return Response.json({ error: "cleanup_failed" }, { status: 500 });
  }
  if (!cleanupResponse.ok) {
    const unauthorized = (await errorCode(cleanupResponse)) === "42501";
    return Response.json(
      { error: unauthorized ? "unauthorized" : "cleanup_failed" },
      { status: unauthorized ? 401 : 500 },
    );
  }

  const data = await cleanupResponse.json();
  const result = (data?.[0] ?? null) as CleanupResult | null;
  if (!result) {
    return Response.json({ error: "cleanup_failed" }, { status: 500 });
  }

  return Response.json({
    ok: true,
    cutoffAt: result.cutoff_at,
    deleted: {
      idempotencyResults: result.idempotency_results_deleted,
      checkpointWrites: result.checkpoint_writes_deleted,
      checkpoints: result.checkpoints_deleted,
      checkpointBlobs: result.checkpoint_blobs_deleted,
      threadMessages: result.thread_messages_deleted,
      threadImages,
    },
  });
});
