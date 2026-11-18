"""Local records of completed questions and MCP queries."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

DEFAULT_LOG = Path(__file__).resolve().parents[2]/'data/agent_queries.jsonl'


def append_query_log(question=None, sql=None, model=None, attempts=1, row_count=None, error=None):
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        record = {
            'timestamp': timestamp,
            'question': question,
            'sql': sql,
            'model': model,
            'attempts': attempts,
            'row_count': row_count,
            'error': error,
        }
        line = json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n'
        path = Path(os.getenv('METROFLOW_QUERY_LOG', str(DEFAULT_LOG)))
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, 'a', encoding='utf-8') as stream:
            stream.write(line)
    except Exception:
        pass
