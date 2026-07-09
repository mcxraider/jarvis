error encountered after running, propose error classes. : python3 agents/agent_api/app/runner.py

[Jarvis Sequential Run 1/1]
---------------------------
runtime.sequence   Starting prompt. | index=1, total=1
couldn't stop thread 'pool-1-worker-0' within 5.0 seconds
couldn't stop thread 'pool-1-worker-1' within 5.0 seconds
Jarvis failed before the graph completed.
pool initialization incomplete after 5.0 sec
Traceback (most recent call last):
  File "/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp/agents/agent_api/app/runner.py", line 428, in main
    results = run_jarvis_sequence(
              ^^^^^^^^^^^^^^^^^^^^
  File "/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp/agents/agent_api/app/runner.py", line 156, in run_jarvis_sequence
    run_jarvis_with_local_clarifications(
  File "/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp/agents/agent_api/app/runner.py", line 98, in run_jarvis_with_local_clarifications
    result = run_jarvis(
             ^^^^^^^^^^^
  File "/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp/agents/agent_api/app/graph/builder.py", line 481, in run_jarvis
    else resolve_runtime_context(identity)
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp/agents/agent_api/app/user_context/resolver.py", line 72, in resolve_runtime_context
    pool = get_pool()
           ^^^^^^^^^^
  File "/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp/agents/agent_api/app/db.py", line 69, in get_pool
    pool.wait(timeout=5.0)
  File "/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp/venv/lib/python3.12/site-packages/psycopg_pool/pool.py", line 163, in wait
    raise PoolTimeout(f"pool initialization incomplete after {timeout} sec")
psycopg_pool.PoolTimeout: pool initialization incomplete after 5.0 sec
