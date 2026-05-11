#!/usr/bin/env python3
"""
pgmcp — command line tool for pg-mcp-server
============================================

Commands:
  create   Create a table from a schema file (CSV, Excel, or text)
  query    Translate plain English to SQL using live table schemas
  tables   List all tables in the database
  schema   Show schema for a specific table

Usage:
  python pgmcp.py create --file schema.csv
  python pgmcp.py create --file schema.xlsx --sheet Sheet1
  python pgmcp.py create --file schema.txt
  python pgmcp.py query "find all orders placed in the last 7 days"
  python pgmcp.py tables
  python pgmcp.py schema orders
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.sse import sse_client
from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser, JsonOutputParser
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Confirm
from rich import print as rprint

load_dotenv()

# ── Config ────────────────────────────────────────────────────
PG_MCP_URL   = os.getenv("PG_MCP_URL",   "http://localhost:8000/sse")
DATABASE_URL = os.getenv("DATABASE_URL",  "postgresql://postgres:password@localhost:5432/mydb")
OLLAMA_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL",  "qwen2.5")

console = Console()


# ── MCP session helper ────────────────────────────────────────

class PgMCP:
    """Thin wrapper around pg-mcp-server for CLI use."""

    def __init__(self, session: ClientSession, conn_id: str):
        self.session  = session
        self.conn_id  = conn_id

    async def query(self, sql: str) -> str:
       result = await self.session.call_tool(
           "pg_query", {"conn_id": self.conn_id, "query": sql}
       )
       if not result.content:
           return ""
       return "\n".join(c.text for c in result.content)

    async def metadata(self, table: str | None = None) -> str:
       if table:
           table = table.strip().strip('"').strip("'") 
           sql = f"""
               SELECT json_agg(row_to_json(t)) as columns
               FROM (
                  SELECT column_name, data_type, character_maximum_length,
                         is_nullable, column_default
                  FROM information_schema.columns
                  WHERE table_schema = 'public'
                  AND table_name = '{table}'
                  ORDER BY ordinal_position
               ) t
           """
       else:
           sql = """
               SELECT json_agg(row_to_json(t)) as columns
               FROM (
                  SELECT table_name, column_name, data_type, is_nullable
                  FROM information_schema.columns
                  WHERE table_schema = 'public'
                  ORDER BY table_name, ordinal_position
               ) t
           """
       return await self.query(sql)

    async def list_tables(self) -> list[str]:
       raw = await self.query(
           "SELECT json_agg(tablename ORDER BY tablename) AS tables "
           "FROM pg_tables WHERE schemaname = 'public'"
       )
       try:
           import json as _json
           parsed = _json.loads(raw)
           if isinstance(parsed, dict) and "tables" in parsed:
               return _json.loads(parsed["tables"]) if isinstance(parsed["tables"], str) else parsed["tables"]
           return []
       except Exception:
           return []


# ── Schema file parsers ───────────────────────────────────────
def _extract_conn_id(result) -> str:
    import json
    raw = result.content[0].text.strip()
    try:
        return json.loads(raw)["conn_id"]
    except Exception:
        return raw

def parse_csv(path: Path) -> list[dict]:
    """
    Expects columns: name, type, [size], [nullable], [default], [primary_key]
    Any column order works — matched by header name.
    """
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalise keys to lowercase
            rows.append({k.strip().lower(): v.strip() for k, v in row.items()})
    return rows


def parse_excel(path: Path, sheet: str | None = None) -> list[dict]:
    try:
        import openpyxl
    except ImportError:
        console.print("[red]openpyxl not installed. Run: pip install openpyxl[/red]")
        sys.exit(1)

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.active

    rows_iter = ws.iter_rows(values_only=True)
    headers   = [str(h).strip().lower() for h in next(rows_iter)]
    rows = []
    for row in rows_iter:
        if not any(row):          # skip empty rows
            continue
        rows.append({headers[i]: (str(v).strip() if v is not None else "") for i, v in enumerate(row)})
    return rows


def parse_text(path: Path) -> list[dict]:
    """
    Flexible plain-text format. One column per line. Examples:
      first_name varchar 100
      age int
      email string 200 not null
      price decimal 10,2
      is_active boolean default true
    Or comma-separated on one line:
      first_name varchar(100), age int, email varchar(200)
    """
    text = path.read_text()

    # Detect single-line comma-separated format
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) == 1 and "," in lines[0]:
        lines = [l.strip() for l in lines[0].split(",")]

    rows = []
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not parts:
            continue
        row = {
            "name": parts[0],
            "type": parts[1] if len(parts) > 1 else "text",
            "size": parts[2] if len(parts) > 2 else "",
        }
        #row["nullable"] = "not null" not in line.lower()
        row["nullable"] = "false" if "not null" in line.lower() else "true"
        rows.append(row)
    return rows


def load_schema_file(path: Path, sheet: str | None = None) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return parse_csv(path)
    elif suffix in (".xlsx", ".xls"):
        return parse_excel(path, sheet)
    elif suffix in (".txt", ".md", ""):
        return parse_text(path)
    else:
        # Try CSV as fallback
        return parse_csv(path)


# ── LLM helpers ───────────────────────────────────────────────

def get_llm(temperature: float = 0.0, json_mode: bool = False) -> ChatOllama:
    kwargs = dict(base_url=OLLAMA_URL, model=OLLAMA_MODEL, temperature=temperature)
    if json_mode:
        kwargs["format"] = "json"
    return ChatOllama(**kwargs)


def generate_create_table(table_name: str, columns: list[dict]) -> str:
    """Ask the LLM to produce a CREATE TABLE from the parsed column list."""
    llm   = get_llm(temperature=0.0)
    chain = ChatPromptTemplate.from_messages([
        ("system", """You are a PostgreSQL DBA. Generate a CREATE TABLE statement.
Rules:
- Use proper PostgreSQL types (VARCHAR, TEXT, INTEGER, BIGINT, NUMERIC, BOOLEAN, TIMESTAMP, UUID, JSONB)
- Add id SERIAL PRIMARY KEY if no primary key is specified
- Add created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() if not already present
- Respect NOT NULL, DEFAULT, and size constraints from the column list
- Return ONLY the raw SQL — no markdown, no explanation"""),
        ("human", "Table name: {table_name}\n\nColumns:\n{columns}"),
    ]) | llm | StrOutputParser()

    col_lines = "\n".join(
        f"  - name={c.get('name','?')}  type={c.get('type','text')}  "
        f"size={c.get('size','')}  nullable={c.get('nullable','true')}  "
        f"default={c.get('default','')}  primary_key={c.get('primary_key','')}"
        for c in columns
    )
    raw = chain.invoke({"table_name": table_name, "columns": col_lines})
    return _strip_fences(raw)


def extract_table_names(request: str) -> list[str]:
    """Ask LLM to extract table names referenced in a plain-English query."""
    llm   = get_llm(temperature=0, json_mode=True)
    chain = ChatPromptTemplate.from_messages([
        ("system", """Extract PostgreSQL table names from the user's request.
Return ONLY JSON: {{"tables": ["table1", "table2"]}}
If no specific table is mentioned return {{"tables": []}}"""),
        ("human", "{request}"),
    ]) | llm | JsonOutputParser()
    result = chain.invoke({"request": request})
    return result.get("tables", [])


def generate_dml(request: str, schemas: dict) -> str:
    # Format schema as a clean readable block instead of raw JSON
    schema_lines = []
    for table, content in schemas.items():
        schema_lines.append(f"Table: {table}")
        try:
            import json as _json
            raw = content if isinstance(content, str) else str(content)
            # Handle the nested JSON string
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict) and "columns" in parsed:
                cols = _json.loads(parsed["columns"]) if isinstance(parsed["columns"], str) else parsed["columns"]
            elif isinstance(parsed, list):
                cols = parsed
            else:
                cols = []
            for col in cols:
                schema_lines.append(f"  - {col['column_name']} ({col['data_type']})")
        except Exception:
            schema_lines.append(f"  {content}")
        schema_lines.append("")
    schema_str = "\n".join(schema_lines)

    llm   = get_llm(temperature=0.0)
    chain = ChatPromptTemplate.from_messages([
        ("system", """You are a PostgreSQL expert. Translate the user's request to SQL.

STRICT RULES — violations will cause errors:
- Use ONLY the exact table names listed in the schema below. Do NOT invent tables.
- Use ONLY the exact column names listed for each table. Do NOT invent columns.
- If a column is nullable, still use it — nullable does not mean the data is missing.
- Only refuse if the required tables or columns genuinely do not exist in the schema.
- UPDATE and DELETE MUST include a WHERE clause.
- Use explicit column names, never SELECT *.
- Return ONLY the raw SQL or an explanation if impossible — no markdown.

AVAILABLE SCHEMA (use nothing else):
{schemas}"""),
        ("human", "{request}"),
    ]) | llm | StrOutputParser()

    raw = chain.invoke({"request": request, "schemas": schema_str})
    return _strip_fences(raw)


def _strip_fences(text: str) -> str:
    text = text.strip()
    # Extract SQL from markdown fences if present
    match = re.search(r"```(?:sql)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    # If no fences, extract just the SQL — everything from SELECT/INSERT/UPDATE/DELETE onwards
    match = re.search(r"(SELECT|INSERT|UPDATE|DELETE|WITH).*", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(0).strip()
    return text.strip()

# ── Commands ──────────────────────────────────────────────────

async def cmd_create(args: argparse.Namespace) -> None:
    path = Path(args.file)
    if not path.exists():
        console.print(f"[red]File not found: {path}[/red]")
        sys.exit(1)

    # ── Parse schema file ─────────────────────────────────────
    with console.status(f"Reading {path.name}…"):
        columns = load_schema_file(path, sheet=getattr(args, "sheet", None))

    if not columns:
        console.print("[red]No columns found in file.[/red]")
        sys.exit(1)

    # ── Determine table name ───────────────────────────────────
    table_name = args.table or path.stem.lower().replace(" ", "_")

    # ── Show what was parsed ───────────────────────────────────
    t = Table(title=f"Schema from {path.name}", show_lines=True)
    headers = list(columns[0].keys())
    for h in headers:
        t.add_column(h.title(), style="cyan")
    for row in columns:
        t.add_row(*[row.get(h, "") for h in headers])
    console.print(t)

    # ── Generate CREATE TABLE ──────────────────────────────────
    with console.status("Generating CREATE TABLE statement…"):
        sql = generate_create_table(table_name, columns)

    console.print(Panel(sql, title=f"CREATE TABLE {table_name}", border_style="yellow"))

    if not Confirm.ask("Execute this statement?"):
        console.print("[dim]Aborted.[/dim]")
        return

    # ── Execute via pg-mcp ────────────────────────────────────
    async with sse_client(PG_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("connect", {"connection_string": DATABASE_URL})
            #conn_id = result.content[0].text.strip()
            conn_id = _extract_conn_id(result)
            pg = PgMCP(session, conn_id)

            try:
                with console.status("Executing…"):
                    out = await pg.query(sql)
                console.print(f"[green]✓ Table '{table_name}' created.[/green]")
                if out:
                    console.print(f"[dim]{out}[/dim]")
            except Exception as exc:
                console.print(f"[red]Execution failed: {exc}[/red]")
            finally:
                await session.call_tool("disconnect", {"conn_id": conn_id})


async def cmd_query(args: argparse.Namespace) -> None:
    request = args.request

    async with sse_client(PG_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("connect", {"connection_string": DATABASE_URL})
            #conn_id = result.content[0].text.strip()
            conn_id = _extract_conn_id(result)
            pg = PgMCP(session, conn_id)

            try:
                # ── Detect table names ────────────────────────
                with console.status("Identifying tables…"):
                    mentioned = extract_table_names(request)

                # ── Fetch schemas ─────────────────────────────
                schemas: dict = {}
                with console.status("Listing all tables…"):
                   table_list = await pg.list_tables()

                with console.status(f"Fetching schemas for: {', '.join(table_list)}…"):
                    for tbl in table_list:
                        try:
                            meta = await pg.metadata(tbl)
                            schemas[tbl] = json.loads(meta) if meta.startswith("{") else meta
                        except Exception:
                            schemas[tbl] = {}

                with console.status(f"Fetching schemas for: {', '.join(table_list)}…"):
                   for tbl in table_list:
                      try:
                         meta = await pg.metadata(tbl)
                         schemas[tbl] = json.loads(meta) if meta.startswith("{") else meta
                      except Exception:
                         schemas[tbl] = {}

                #DEBUG
                console.print(f"[dim]Tables fetched: {list(schemas.keys())}[/dim]")
                for t, v in schemas.items():
                   console.print(f"[dim]{t}: {str(v)[:100]}[/dim]")

                if not schemas:
                    console.print("[yellow]No table schemas found. Is the database populated?[/yellow]")
                    return

                # ── Generate SQL ──────────────────────────────
                with console.status("Generating SQL…"):
                    sql = generate_dml(request, schemas)

                console.print(Panel(sql, title="Generated SQL", border_style="yellow"))

                action = _prompt_action()

                if action == "e":   # execute
                    with console.status("Executing…"):
                        out = await pg.query(sql)
                    #console.print(Panel(out or "(no rows)", title="Result", border_style="green"))
                    if out:
                        t = Table(show_header=True, header_style="bold cyan")
                        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
                        if rows:
                           for col in rows[0].keys():
                               t.add_column(col)
                           for row in rows:
                               t.add_row(*[str(v) for v in row.values()])
                           console.print(t)
                    else:
                       console.print("[dim](no rows)[/dim]")

                elif action == "r": # revise
                    revision = input("Describe the change: ").strip()
                    with console.status("Revising…"):
                        schemas_with_note = dict(schemas)
                        revised = generate_dml(f"{request}. Also: {revision}", schemas)
                    console.print(Panel(revised, title="Revised SQL", border_style="yellow"))
                    if Confirm.ask("Execute revised SQL?"):
                        with console.status("Executing…"):
                            out = await pg.query(revised)
                        console.print(Panel(out or "(no rows)", title="Result", border_style="green"))

                else:
                    console.print("[dim]Aborted.[/dim]")

            finally:
                await session.call_tool("disconnect", {"conn_id": conn_id})


async def cmd_tables(args: argparse.Namespace) -> None:
    async with sse_client(PG_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("connect", {"connection_string": DATABASE_URL})
            #conn_id = result.content[0].text.strip()
            conn_id = _extract_conn_id(result)
            pg = PgMCP(session, conn_id)
            try:
                with console.status("Fetching tables…"):
                    tables = await pg.list_tables()
                t = Table(title="Tables in public schema")
                t.add_column("Table name", style="cyan")
                for tbl in tables:
                    t.add_row(tbl)
                console.print(t)
            finally:
                await session.call_tool("disconnect", {"conn_id": conn_id})


async def cmd_schema(args: argparse.Namespace) -> None:
    async with sse_client(PG_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("connect", {"connection_string": DATABASE_URL})
            #conn_id = result.content[0].text.strip()
            conn_id = _extract_conn_id(result)
            pg = PgMCP(session, conn_id)
            try:
                with console.status(f"Fetching schema for {args.table}…"):
                    meta = await pg.metadata(args.table)
                console.print(Panel(
                    json.dumps(json.loads(meta), indent=2) if meta.startswith("{") else meta,
                    title=f"Schema: {args.table}",
                    border_style="cyan",
                ))
            finally:
                await session.call_tool("disconnect", {"conn_id": conn_id})


# ── UI helpers ────────────────────────────────────────────────

def _prompt_action() -> str:
    console.print(
        "\n[bold]What would you like to do?[/bold]\n"
        "  [cyan]e[/cyan] — Execute\n"
        "  [cyan]r[/cyan] — Revise\n"
        "  [cyan]a[/cyan] — Abort\n"
    )
    while True:
        choice = input("Choice [e/r/a]: ").strip().lower()
        if choice in ("e", "r", "a"):
            return choice


# ── CLI wiring ────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pgmcp",
        description="Command line tool for pg-mcp-server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pgmcp.py create --file schema.csv
  python pgmcp.py create --file schema.xlsx --sheet Sheet1 --table orders
  python pgmcp.py create --file schema.txt --table products
  python pgmcp.py query "find all customers who signed up last month"
  python pgmcp.py query "total revenue per product category this year"
  python pgmcp.py tables
  python pgmcp.py schema orders
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = sub.add_parser("create", help="Create a table from a schema file")
    p_create.add_argument("--file",  "-f", required=True, help="Schema file (.csv, .xlsx, .txt)")
    p_create.add_argument("--sheet", "-s", help="Excel sheet name (optional)")
    p_create.add_argument("--table", "-t", help="Table name (default: filename stem)")

    # query
    p_query = sub.add_parser("query", help="Translate plain English to SQL")
    p_query.add_argument("request", help="Plain English request in quotes")

    # tables
    sub.add_parser("tables", help="List all tables in the database")

    # schema
    p_schema = sub.add_parser("schema", help="Show schema for a table")
    p_schema.add_argument("table", help="Table name")

    return parser


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    handlers = {
        "create": cmd_create,
        "query":  cmd_query,
        "tables": cmd_tables,
        "schema": cmd_schema,
    }
    asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    main()
