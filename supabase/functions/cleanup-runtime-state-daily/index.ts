import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "@supabase/supabase-js";

type CleanupResult = {
  cutoff_at: string;
  idempotency_results_deleted: number;
  checkpoint_writes_deleted: number;
  checkpoints_deleted: number;
  checkpoint_blobs_deleted: number;
};

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

  const supabase = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { data, error } = await supabase.rpc("cleanup_runtime_state_daily", {
    p_cron_secret: cronSecret,
  });

  if (error) {
    const unauthorized = error.code === "42501";
    return Response.json(
      { error: unauthorized ? "unauthorized" : "cleanup_failed" },
      { status: unauthorized ? 401 : 500 },
    );
  }

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
    },
  });
});
