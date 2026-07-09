import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2";

const TIME_ZONE = "Asia/Singapore";

function singaporeDate(date: Date): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function previousSingaporeDay(): string {
  const today = singaporeDate(new Date());
  const midnightUtc = new Date(`${today}T00:00:00Z`);
  midnightUtc.setUTCDate(midnightUtc.getUTCDate() - 1);
  return midnightUtc.toISOString().slice(0, 10);
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
    console.error("usage_daily.snapshot.missing_environment");
    return Response.json({ error: "server_misconfigured" }, { status: 500 });
  }

  const day = previousSingaporeDay();
  const supabase = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { data: rows, error } = await supabase.rpc("snapshot_usage_daily", {
    p_day: day,
    p_cron_secret: cronSecret,
  });

  if (error) {
    const unauthorized = error.code === "42501";
    console.error("usage_daily.snapshot.failed", {
      day,
      code: error.code,
      message: unauthorized ? "unauthorized" : error.message,
    });
    return Response.json(
      { error: unauthorized ? "unauthorized" : "snapshot_failed" },
      { status: unauthorized ? 401 : 500 },
    );
  }

  console.log("usage_daily.snapshot.completed", { day, rows });
  return Response.json({ ok: true, day, rows });
});
