"""Local stdio tools for Gold queries and table context."""
from mcp.server import MCPServer

from metroflow.agent.context import load_context
from metroflow.agent.query import run_sql as execute_sql
from metroflow.agent.query_log import append_query_log

server = MCPServer('MetroFlow')


@server.tool()
def run_sql(query: str) -> dict:
    """Run one bounded, read-only DuckDB SELECT over the allowed Gold tables."""
    record = {'sql': query, 'attempts': 1, 'row_count': None, 'error': None}
    try:
        result = execute_sql(query)
        row_count = None
        if 'rows' in result:
            row_count = len(result['rows'])
        record.update(sql=result.get('sql', query), error=result.get('error'), row_count=row_count)
        return result
    except Exception as exc:
        record['error'] = str(exc)
        raise
    finally:
        append_query_log(**record)


@server.tool()
def describe_tables() -> dict:
    """Return the Gold table descriptions, grains, columns and join keys."""
    return load_context()


if __name__ == '__main__':
    server.run(transport='stdio')
