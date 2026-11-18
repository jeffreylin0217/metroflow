"""Read-only execution with bounded time and JSON-safe results."""
import math
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Timer

import duckdb
from sqlglot.errors import SqlglotError

from metroflow.agent.guardrails import validate_sql

WAREHOUSE = Path(__file__).resolve().parents[2]/'data/warehouse.duckdb'


def json_safe(value):
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    return value


def run_sql(query, database=WAREHOUSE, timeout=20):
    try:
        sql = validate_sql(query)
        with duckdb.connect(str(database), read_only=True,
                            config={'enable_external_access': False}) as connection:
            timer = Timer(timeout, connection.interrupt)
            timer.daemon = True
            timer.start()
            try:
                cursor = connection.execute(sql)
                columns = [column[0] for column in cursor.description]
                rows = cursor.fetchmany(51)
            finally:
                timer.cancel()
                timer.join()
        return {'sql': sql, 'columns': columns, 'rows': json_safe(rows[:50]),
                'truncated': len(rows) > 50}
    except duckdb.InterruptException as exc:
        return {'error': f'Query exceeded {timeout:g} seconds: {exc}'}
    except (ValueError, SqlglotError, duckdb.Error) as exc:
        error = str(exc)
        if 'lock' in error.lower():
            error = 'Warehouse is locked; wait for the pipeline to finish and retry. ' + error
        return {'error': error}
