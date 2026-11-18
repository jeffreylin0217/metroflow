# Asking my warehouse questions in plain English

I wanted to know if a small model running on my laptop could answer real questions about my MetroFlow warehouse, and do it safely. So I built a small agent on top of the Gold tables and measured how often it gets the right answer.

## How it works

The agent sends the question to a local model along with a short YAML description of each Gold table: what one row means, the important columns, and how the tables join. The model has one tool, run_sql. It writes a query, my code checks it, DuckDB runs it, and the model answers from the rows that come back. If the query fails, or the model tries to answer without running SQL, it gets the error back and two more tries.

A few rules in the context came from real mistakes. The marts are daily summaries, so trips are counted with SUM(trip_count) instead of COUNT(*). Zone names live in dim_zone. Retained trips include both ACCEPTED and SUSPICIOUS rows, and only mart_quality counts INVALID ones.

## Keeping it safe

Every query goes through these checks before DuckDB sees it:

- It has to be a single SELECT. CTEs and subqueries are fine, but nothing that writes data, changes settings, attaches files, or reads from disk.
- It can only touch the 7 Gold tables. Bronze, staging, and anything else is rejected, even inside a CTE.
- Only a short list of normal SQL functions is allowed.
- The warehouse is opened read only with external access turned off.
- Results are capped at 200 rows, the model sees at most 50, and a query that runs longer than 20 seconds gets stopped.

## Running it

Use the repo's Python 3.12 venv, start Ollama, and pull granite4:3b:

```bash
.venv/bin/python -m pip install -r requirements-agent.txt
.venv/bin/python -m metroflow.agent "How many retained trips are in the warehouse in total?" --show-sql
.venv/bin/python -m pytest -q
.venv/bin/python scripts/eval_agent.py --output /tmp/metroflow-agent-eval.json
```

The model, base URL, and API key come from OPENAI_MODEL, OPENAI_BASE_URL, and OPENAI_API_KEY, so the same code can point at another endpoint. If you do that, the endpoint sees the question, the table context, and the returned rows. The unit tests use a fake model, so CI never calls a real one.

## How I measured it

I wrote 15 questions with reference SQL. The eval runs the agent's final SQL and the reference SQL and compares the results, ignoring column names and row order and allowing a 0.01 difference on numbers. It grades the query results, not the written answer.

Granite got 7 of the 15 questions right.

## Limits

This is a small local eval, 7 of 15 questions with Granite. A query can run fine and still answer the wrong question, so I still read the SQL before trusting an answer.
