"""Dump domain_router.classify.openai LLM runs from LangSmith to parquet.

Usage (venv active, from project root):
    python tests/dump_domain_router_runs.py
Env: LANGSMITH_API_KEY, LANGSMITH_PROJECT (default "jarvis").
"""

import os

import pandas as pd
from dotenv import load_dotenv
from langsmith import Client

load_dotenv()

PROJECT = os.environ.get("LANGSMITH_PROJECT", "jarvis")
OUT = os.environ.get("ROUTER_RUNS_OUT", "domain_router_runs.parquet")

client = Client()

rows = []

for run in client.list_runs(project_name=PROJECT, run_type="llm"):
    if run.name != "domain_router.classify.openai":
        continue

    rows.append(
        {
            "run_id": str(run.id),
            "trace_id": str(run.trace_id),
            "parent_run_id": (str(run.parent_run_id) if run.parent_run_id else None),
            "start_time": run.start_time,
            "end_time": run.end_time,
            "inputs": run.inputs,
            "outputs": run.outputs,
            "error": run.error,
            "tags": run.tags,
            "extra": run.extra,
        }
    )

df = pd.DataFrame(rows)
# ponytail: dict/list columns are stringified so arrow can type them; parse back with ast.literal_eval.
for col in ("inputs", "outputs", "tags", "extra"):
    if col in df.columns:
        df[col] = df[col].map(repr)

df.to_parquet(OUT, index=False)
print(f"project={PROJECT} runs={len(df)} -> {OUT}")
if not df.empty:
    print(df[["run_id", "start_time", "error"]].head(10).to_string(index=False))
