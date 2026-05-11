#!/usr/bin/env python3
"""
seed.py — populate customers, products and orders with sample data

Usage:
  python seed.py              # seed all tables
  python seed.py customers    # seed one table
  python seed.py products orders  # seed specific tables
"""
import asyncio
import os
import sys
import json

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.sse import sse_client
from rich.console import Console

load_dotenv()

PG_MCP_URL   = os.getenv("PG_MCP_URL",   "http://localhost:8000/sse")
DATABASE_URL = os.getenv("DATABASE_URL",  "postgresql://postgres@localhost:5432/postgres")

console = Console()

# ── Seed data ─────────────────────────────────────────────────

SEEDS = {
    "customers": """
        INSERT INTO customers (first_name, last_name, email, phone, date_of_birth, is_active)
        VALUES
        ('Alice',   'Johnson',  'alice@example.com',   '555-1001', '1990-03-15', true),
        ('Bob',     'Smith',    'bob@example.com',     '555-1002', '1985-07-22', true),
        ('Carol',   'Williams', 'carol@example.com',   '555-1003', '1992-11-08', true),
        ('David',   'Brown',    'david@example.com',   '555-1004', '1988-01-30', false),
        ('Eva',     'Davis',    'eva@example.com',     '555-1005', '1995-06-17', true),
        ('Frank',   'Miller',   'frank@example.com',   '555-1006', '1983-09-25', true),
        ('Grace',   'Wilson',   'grace@example.com',   '555-1007', '1991-04-12', false),
        ('Henry',   'Moore',    'henry@example.com',   '555-1008', '1987-12-03', true),
        ('Iris',    'Taylor',   'iris@example.com',    '555-1009', '1993-08-19', true),
        ('James',   'Anderson', 'james@example.com',   '555-1010', '1986-02-28', true)
    """,

    "products": """
        INSERT INTO products (name, description, price, stock, sku, is_active, category)
        VALUES
        ('Laptop Pro 15',     'High performance laptop',      1299.99,  45, 'LAP-001', true,  'Electronics'),
        ('Wireless Mouse',    'Ergonomic wireless mouse',        29.99, 200, 'MOU-001', true,  'Accessories'),
        ('USB-C Hub',         '7-in-1 USB-C hub',                49.99, 150, 'HUB-001', true,  'Accessories'),
        ('4K Monitor',        '27 inch 4K display',             399.99,  30, 'MON-001', true,  'Electronics'),
        ('Mechanical Kbd',    'Mechanical keyboard RGB',          89.99,  80, 'KBD-001', true,  'Accessories'),
        ('Webcam HD',         '1080p webcam with mic',            69.99,  60, 'CAM-001', true,  'Electronics'),
        ('Desk Lamp',         'LED desk lamp dimmable',           34.99, 120, 'LMP-001', true,  'Office'),
        ('Standing Desk',     'Adjustable standing desk',        449.99,  15, 'DSK-001', true,  'Office'),
        ('Ergonomic Chair',   'Lumbar support office chair',     299.99,  20, 'CHR-001', false, 'Office'),
        ('Noise Headphones',  'ANC over-ear headphones',         199.99,  55, 'HDP-001', true,  'Electronics')
    """,

    "orders": """
        INSERT INTO orders (customer_id, status, total, notes, shipped_at)
        VALUES
        (1,  'delivered', 1329.98, 'Bundle deal',         NOW() - INTERVAL '10 days'),
        (2,  'shipped',    449.99, NULL,                  NOW() - INTERVAL '2 days'),
        (3,  'pending',     79.98, 'Gift wrap requested', NULL),
        (4,  'delivered',   49.99, NULL,                  NOW() - INTERVAL '20 days'),
        (5,  'cancelled',  299.99, 'Customer cancelled',  NULL),
        (1,  'delivered',   89.99, NULL,                  NOW() - INTERVAL '5 days'),
        (6,  'shipped',    469.98, NULL,                  NOW() - INTERVAL '1 day'),
        (7,  'pending',    199.99, 'Express shipping',    NULL),
        (8,  'delivered',   34.99, NULL,                  NOW() - INTERVAL '15 days'),
        (9,  'shipped',    399.99, NULL,                  NOW() - INTERVAL '3 days'),
        (10, 'pending',    119.98, NULL,                  NULL),
        (2,  'delivered',   69.99, NULL,                  NOW() - INTERVAL '8 days')
    """,

    "order_items": """
         INSERT INTO order_items (order_id, product_id, quantity, unit_price)
         VALUES
         (1,  1,  1, 1299.99),
         (1,  2,  1,   29.99),
         (2,  8,  1,  449.99),
         (3,  2,  1,   29.99),
         (3,  5,  1,   89.99),
         (4,  3,  1,   49.99),
         (5,  9,  1,  299.99),
         (6,  5,  1,   89.99),
         (7,  10, 1,  199.99),
         (8,  7,  1,   34.99),
         (9,  4,  1,  399.99),
         (10, 2,  1,   29.99),
         (10, 6,  1,   69.99),
         (11, 2,  2,   29.99),
         (11, 5,  1,   89.99),
         (12, 6,  1,   69.99)
    """,
}


# ── Helpers ───────────────────────────────────────────────────

def _extract_conn_id(result) -> str:
    raw = result.content[0].text.strip()
    try:
        return json.loads(raw)["conn_id"]
    except Exception:
        return raw


async def run(tables: list[str]) -> None:
    async with sse_client(PG_MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "connect", {"connection_string": DATABASE_URL}
            )
            conn_id = _extract_conn_id(result)

            try:
                for table in tables:
                    if table not in SEEDS:
                        console.print(f"[yellow]No seed data defined for '{table}' — skipping.[/yellow]")
                        continue

                    with console.status(f"Seeding {table}…"):
                        await session.call_tool(
                            "pg_query",
                            {"conn_id": conn_id, "query": SEEDS[table].strip()}
                        )
                    console.print(f"[green]✓ {table} seeded.[/green]")

                # Row counts
                console.print()
                console.print("[bold]Row counts:[/bold]")
                for table in tables:
                    if table in SEEDS:
                        result = await session.call_tool(
                            "pg_query",
                            {"conn_id": conn_id, "query": f"SELECT COUNT(*) FROM {table}"}
                        )
                        count = result.content[0].text.strip() if result.content else "?"
                        console.print(f"  {table:20s} {count} rows")

            finally:
                await session.call_tool("disconnect", {"conn_id": conn_id})


def main() -> None:
    requested = sys.argv[1:]
    tables = requested if requested else list(SEEDS.keys())

    console.rule("[bold]Seeding database[/bold]")
    console.print(f"Tables: {', '.join(tables)}\n")

    asyncio.run(run(tables))


if __name__ == "__main__":
    main()
