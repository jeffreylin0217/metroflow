"""Validate a single bounded Gold SELECT before DuckDB sees it."""
import sqlglot
from sqlglot import exp

from metroflow.agent.context import TABLES

FUNCTIONS = set('AND OR COUNT SUM AVG MIN MAX ROUND ABS CEIL FLOOR CAST TRY_CAST COALESCE NULLIF '
                'IF CASE EXTRACT DATE_TRUNC STRFTIME TIME_TO_STR YEAR MONTH DAY DAY_OF_WEEK '
                'DAY_OF_MONTH DAY_OF_YEAR LOWER UPPER TRIM LENGTH CONCAT CONCAT_WS '
                'LAG LEAD ROW_NUMBER RANK DENSE_RANK FIRST_VALUE LAST_VALUE '
                'STDDEV_SAMP STDDEV_POP VAR_SAMP VAR_POP CORR POWER SQRT '.split())


def cte_is_visible(table):
    child = table
    while child.parent is not None:
        parent = child.parent
        if isinstance(parent, exp.With):
            position = parent.expressions.index(child)
            definitions = parent.expressions[:position]
            if parent.args.get('recursive') and isinstance(child.this, exp.SetOperation):
                definitions = definitions + [child]
        else:
            with_clause = parent.args.get('with_')
            definitions = []
            if with_clause is not None and with_clause is not child:
                definitions = with_clause.expressions
        for cte in definitions:
            if cte.alias.lower() == table.name.lower():
                return True
        child = parent
    return False


def validate_sql(query):
    statements = sqlglot.parse(query, read='duckdb')
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError('exactly one SELECT statement is allowed')
    tree = statements[0]
    for node in tree.walk():
        if isinstance(node, (exp.DDL, exp.DML, exp.Command, exp.Into)):
            raise ValueError('only read-only SELECT expressions are allowed')
    for function in tree.find_all(exp.Func):
        if isinstance(function, exp.Anonymous):
            name = function.name.upper()
        else:
            name = function.sql_name()
        if name not in FUNCTIONS or isinstance(function.parent, exp.Dot):
            raise ValueError(f'function is not allowed: {name}')
    cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        plain_name = isinstance(table.this, exp.Identifier)
        if plain_name and not table.db and not table.catalog:
            if table.name.lower() in cte_names and cte_is_visible(table):
                continue
        schema = table.db or 'main'
        name = f'{schema}.{table.name}'.lower()
        if not plain_name or table.catalog or name not in TABLES:
            raise ValueError(f'table is not allowed: {table.sql(dialect="duckdb")}')
        table.set('db', exp.to_identifier('main'))
    limit = tree.args.get('limit')
    if limit:
        value = limit.expression
        if not isinstance(value, exp.Literal) or value.is_string or not value.this.isdigit():
            raise ValueError('LIMIT must be a nonnegative integer')
        tree = tree.limit(min(int(value.this), 200))
    else:
        tree = tree.limit(200)
    return tree.sql(dialect='duckdb')
