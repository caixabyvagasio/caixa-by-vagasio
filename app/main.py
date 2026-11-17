from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3

try:
    import psycopg2
    from psycopg2.extras import DictCursor
except ImportError:  # SQLite remains available for tests and local development.
    psycopg2 = None
    DictCursor = None

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
POSTGRES_BACKEND = DATABASE_URL.startswith(("postgres://", "postgresql://"))
REQUIRE_POSTGRES = os.environ.get("CAIXA_REQUIRE_POSTGRES", "false").strip().lower() == "true"


class HybridRow(dict):
    """PostgreSQL mapping rows that also support the existing [0] reads."""
    def __init__(self, cursor, values):
        super().__init__(zip((d[0] for d in cursor.description), values))
        self._keys = tuple(self.keys())
    def __getitem__(self, key):
        return super().__getitem__(self._keys[key] if isinstance(key, int) else key)


class HybridCursor:
    def __init__(self, cursor): self._cursor=cursor
    def fetchone(self):
        values=self._cursor.fetchone()
        return HybridRow(self._cursor, values) if values is not None and POSTGRES_BACKEND else values
    def fetchall(self):
        rows=self._cursor.fetchall()
        return [HybridRow(self._cursor,row) for row in rows] if POSTGRES_BACKEND else rows
    def __iter__(self):
        for row in self._cursor:
            yield HybridRow(self._cursor,row) if POSTGRES_BACKEND else row
    @property
    def rowcount(self): return self._cursor.rowcount


class PostgresConnection:
    def __init__(self, raw): self.raw=raw; self.row_factory=None; self._transaction_open=False
    def _sql(self, sql):
        raw_sql=sql.strip()
        stripped=raw_sql
        pragma=re.fullmatch(r"PRAGMA table_info\((\w+)\)",stripped,re.I)
        if pragma:
            table=pragma.group(1)
            return "SELECT (ordinal_position-1) AS cid,column_name AS name,data_type AS type,(is_nullable='NO')::int AS notnull,NULL AS dflt_value,0 AS pk FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position", (table,)
        if re.match(r"PRAGMA (foreign_keys|busy_timeout|journal_mode)",stripped,re.I):
            return "SELECT 1", None
        if "INSERT OR REPLACE INTO SEGMENT_CATALOG" in raw_sql.upper():
            return "INSERT INTO segment_catalog(id,name,questions_json,active) VALUES(%s,%s,%s,1) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,questions_json=EXCLUDED.questions_json,active=1", None
        if "INSERT OR REPLACE INTO FEATURE_CATALOG" in raw_sql.upper():
            return "INSERT INTO feature_catalog(id,name,description,category,recommended_segments_json,dependencies_json,available) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,description=EXCLUDED.description,category=EXCLUDED.category,recommended_segments_json=EXCLUDED.recommended_segments_json,dependencies_json=EXCLUDED.dependencies_json,available=EXCLUDED.available", None
        if "ON CONFLICT(COMPANY_ID) DO UPDATE" in raw_sql.upper():
            return re.sub(r"ON CONFLICT\(company_id\) DO UPDATE SET (.*)",lambda m:"ON CONFLICT(company_id) DO UPDATE SET "+re.sub(r"excluded\.([a-z_]+)",r"EXCLUDED.\1",m.group(1),flags=re.I),sql.replace("?","%s"),flags=re.I|re.S), None
        ignore_insert=bool(re.search(r"^\s*INSERT OR IGNORE INTO",sql,re.I))
        sql=re.sub(r"^\s*INSERT OR IGNORE INTO", "INSERT INTO",sql,flags=re.I)
        sql=sql.replace("?", "%s")
        sql=sql.replace("BEGIN IMMEDIATE", "BEGIN")
        if ignore_insert and "ON CONFLICT" not in sql.upper():
            sql=sql.rstrip().rstrip(';')+" ON CONFLICT DO NOTHING"
        if re.match(r"^(CREATE TABLE|ALTER TABLE)",sql.strip(),re.I):
            sql=re.sub(r"\bINTEGER\b","BIGINT",sql,flags=re.I)
        sql=re.sub(r"date\('now'\)","CURRENT_DATE",sql,flags=re.I)
        sql=re.sub(r"strftime\('%Y-%m',\s*([a-zA-Z0-9_.]+)\)",r"to_char(CAST(SUBSTRING(\1 FROM 1 FOR 10) AS date),'YYYY-MM')",sql,flags=re.I)
        sql=re.sub(r"date\(([a-zA-Z0-9_.]+)\)",r"CAST(SUBSTRING(\1 FROM 1 FOR 10) AS date)",sql,flags=re.I)
        sql=sql.replace("date(%s)","CAST(%s AS date)")
        sql=re.sub(r"([a-zA-Z0-9_.]+)\s*<\s*CURRENT_DATE",r"CAST(\1 AS date)<CURRENT_DATE",sql,flags=re.I)
        sql=re.sub(r"date\(([^)]+)\)\s*=\s*CURRENT_DATE",r"CAST(SUBSTRING(\1 FROM 1 FOR 10) AS date)=CURRENT_DATE",sql,flags=re.I)
        sql=re.sub(r"SUM\(MAX\(0,", "SUM(GREATEST(0,",sql,flags=re.I)
        sql=re.sub(r"\s+COLLATE\s+NOCASE\b", "", sql, flags=re.I)
        return sql, None
    def execute(self, sql, params=()):
        rewritten, special_params=self._sql(sql)
        if re.match(r"^\s*BEGIN\b",rewritten,re.I): self._transaction_open=True
        cur=self.raw.cursor(cursor_factory=DictCursor)
        try:
            cur.execute(rewritten,special_params if special_params is not None else params)
            if self._transaction_open and rewritten.lstrip().upper().startswith(("COMMIT","ROLLBACK")): self._transaction_open=False
        except Exception as exc:
            cur.close()
            if psycopg2 and isinstance(exc,psycopg2.IntegrityError):
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise
        return HybridCursor(cur)
    def executemany(self,sql,seq):
        rewritten,_=self._sql(sql);cur=self.raw.cursor(cursor_factory=DictCursor);cur.executemany(rewritten,seq);return HybridCursor(cur)
    def executescript(self,script):
        cleaned=re.sub(r"CREATE TRIGGER IF NOT EXISTS .*?;\s*END;", "",script,flags=re.I|re.S)
        for statement in cleaned.split(';'):
            if statement.strip(): self.execute(statement)
    def commit(self):
        if self._transaction_open:
            cur=self.raw.cursor();cur.execute("COMMIT");cur.close();self._transaction_open=False
    def rollback(self):
        if self._transaction_open:
            cur=self.raw.cursor();cur.execute("ROLLBACK");cur.close();self._transaction_open=False
    def close(self): self.raw.close()


def install_postgres_immutability(conn):
    if not is_postgres(conn): return
    conn.execute("CREATE OR REPLACE FUNCTION caixa_reject_immutable_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only record' USING ERRCODE='23000'; END; $$")
    for table in ("audit_events","payable_payments","company_profile_versions","restaurant_events","restaurant_tab_payments"):
        conn.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_write ON {table}")
        conn.execute(f"CREATE TRIGGER {table}_immutable_write BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION caixa_reject_immutable_change()")


def is_postgres(conn=None):
    return isinstance(conn,PostgresConnection) if conn is not None else POSTGRES_BACKEND


def integrity_errors():
    return (sqlite3.IntegrityError, psycopg2.IntegrityError) if psycopg2 else (sqlite3.IntegrityError,)

import time
import uuid
import threading
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

APP_VERSION = "0.1.0"
DB_PATH = os.environ.get("CAIXA_DB_PATH", "data/caixa.sqlite3")
TOKEN_SECRET = os.environ.get("CAIXA_TOKEN_SECRET", "")
BOOTSTRAP_SECRET = os.environ.get("CAIXA_BOOTSTRAP_SECRET", "")
DEMO_MODE = os.environ.get("CAIXA_DEMO_MODE", "").strip().lower() == "true"
DEMO_COMPANY_ID = "00000000-0000-4000-8000-000000000001"
DEMO_OWNER_ID = "00000000-0000-4000-8000-000000000002"
TOKEN_TTL_SECONDS = int(os.environ.get("CAIXA_TOKEN_TTL_SECONDS", "28800"))
PUBLIC_SIGNUP = os.environ.get("CAIXA_PUBLIC_SIGNUP", "false").strip().lower() == "true"
ROLES = {"owner", "admin", "manager", "operator", "stock"}


class WindowRateLimiter:
    """Small process-local abuse guard; production must also rate-limit at the edge."""
    def __init__(self):
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - seconds:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            if len(self._events) > 20_000:
                for k in [k for k,v in self._events.items() if not v or v[-1] <= now - 3600][:5000]:
                    self._events.pop(k, None)
            return True


_RATE_LIMITER = WindowRateLimiter()


def enforce_rate_limit(key: str, limit: int, seconds: int) -> None:
    if not _RATE_LIMITER.allow(key, limit, seconds):
        raise HTTPException(429, "Muitas tentativas. Aguarde um pouco e tente novamente.",
                            headers={"Retry-After": str(seconds)})


SEGMENT_CATALOG = {
    "retail": {"name": "Varejo geral", "questions": [
        {"id":"barcode","label":"Utiliza leitura de código de barras?","type":"boolean"},
        {"id":"multiple_branches","label":"Possui ou pretende operar várias filiais?","type":"boolean"}]},
    "grocery": {"name": "Supermercados e mercearias", "questions": [
        {"id":"barcode","label":"Utiliza leitores de código de barras?","type":"boolean"},
        {"id":"weighed_items","label":"Vende produtos pesáveis?","type":"boolean"},
        {"id":"perishables","label":"Controla validade ou produtos perecíveis?","type":"boolean"}]},
    "restaurant": {"name": "Restaurantes e lanchonetes", "questions": [
        {"id":"tables","label":"Trabalha com mesas?","type":"boolean"},
        {"id":"tabs","label":"Utiliza comandas?","type":"boolean"},
        {"id":"delivery","label":"Oferece delivery?","type":"boolean"},
        {"id":"kitchen","label":"Possui cozinha ou área de preparo?","type":"boolean"}]},
    "apparel": {"name": "Lojas de roupas e calçados", "questions": [
        {"id":"sizes_colors","label":"Trabalha com tamanhos e cores?","type":"boolean"},
        {"id":"returns","label":"Realiza trocas e devoluções?","type":"boolean"},
        {"id":"commission","label":"Utiliza vendedores comissionados?","type":"boolean"}]},
    "services": {"name": "Prestadores de serviços", "questions": [
        {"id":"appointments","label":"Trabalha com agendamentos?","type":"boolean"},
        {"id":"professionals","label":"Possui profissionais com agenda própria?","type":"boolean"}]},
    "construction": {"name": "Materiais de construção", "questions": [
        {"id":"measurement","label":"Vende por unidade, metragem ou medida?","type":"boolean"},
        {"id":"delivery","label":"Organiza entregas de materiais?","type":"boolean"}]},
}
FEATURES = [
    ("sales", "Vendas e PDV", "Operação universal de vendas e caixa", "core", ["*"], []),
    ("inventory", "Estoque e movimentações", "Saldo com histórico auditável", "core", ["*"], []),
    ("barcode", "Leitura de código de barras", "Busca de produtos por código; hardware requer validação", "operations", ["grocery","retail"], []),
    ("weighed_items", "Produtos pesáveis", "Cadastro e venda por quantidade fracionada; balança não integrada", "operations", ["grocery"], []),
    ("expiry_control", "Validade e perecíveis", "Estrutura inicial para controle de validade", "inventory", ["grocery"], ["inventory"]),
    ("tables_tabs", "Mesas e comandas", "Comandas, pedidos e divisão de contas ainda em implementação", "restaurant", ["restaurant"], ["sales"]),
    ("restaurant_floor", "Ambientes, mesas e reservas", "Organize áreas e mesas, acompanhe ocupação e registre reservas com validação de capacidade e conflito", "restaurant", ["restaurant"], ["sales"]),
    ("delivery", "Delivery e retirada", "Opção de atendimento; integração externa não configurada", "restaurant", ["restaurant"], ["sales"]),
    ("kitchen", "Pedidos de preparo", "Preparação e cozinha; integração com impressoras pendente", "restaurant", ["restaurant"], ["sales"]),
    ("variants", "Tamanhos e cores", "Variações de produto; cadastro especializado ainda não incluído", "catalog", ["apparel"], ["inventory"]),
    ("returns", "Trocas e devoluções", "Regras de troca ainda dependem de implementação", "sales", ["apparel","retail"], ["sales"]),
    ("commissions", "Comissões de vendedores", "Apuração ainda depende do módulo de vendas", "sales", ["apparel","services"], ["sales"]),
    ("appointments", "Agendamentos", "Agenda especializada ainda não incluída", "services", ["services"], []),
    ("measurement_sales", "Vendas por medida", "Unidades comerciais especializadas ainda dependem de regras", "catalog", ["construction"], ["inventory"]),
    ("purchasing", "Compras e fornecedores", "Fornecedores, pedidos e recebimentos vinculados ao estoque", "management", ["*"], ["inventory"]),
    ("customers", "Clientes", "Cadastro e consulta de clientes", "management", ["*"], []),
    ("finance", "Financeiro", "Despesas, contas a pagar e movimentações registradas", "management", ["*"], []),
    ("reports", "Relatórios", "Relatórios de vendas e posição de estoque", "management", ["*"], []),
    ("staff", "Equipe e acessos", "Contas e permissões da equipe", "security", ["*"], []),
    ("profile_setup", "Perfil do negócio", "Atividades e configuração operacional", "configuration", ["*"], []),
]

IMPLEMENTED_FEATURES = {"sales", "inventory", "purchasing", "customers", "finance", "reports", "staff", "profile_setup"}
AVAILABLE_FEATURES = IMPLEMENTED_FEATURES | {"restaurant_floor"}

def now_iso() -> str:
    # Keep deterministic ordering for sequential audit/profile events created within one second.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id() -> str:
    return str(uuid.uuid4())


def get_db_path() -> str:
    path = Path(DB_PATH)
    if DB_PATH != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def connect_db():
    if POSTGRES_BACKEND:
        if psycopg2 is None:
            raise RuntimeError("DATABASE_URL PostgreSQL exige psycopg2-binary instalado.")
        sslmode="require" if ".render.com" in DATABASE_URL else "prefer"
        raw=psycopg2.connect(DATABASE_URL,connect_timeout=10,sslmode=sslmode)
        raw.autocommit=True
        return PostgresConnection(raw)
    conn = sqlite3.connect(get_db_path(), timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def require_persistent_database():
    if not POSTGRES_BACKEND:
        raise RuntimeError("DATABASE_URL PostgreSQL não configurada: o serviço não pode iniciar com SQLite efêmero.")
    if psycopg2 is None:
        raise RuntimeError("DATABASE_URL existe, mas psycopg2-binary não está instalado.")


def set_transaction_mode(conn, mode):
    conn.execute("BEGIN" if is_postgres(conn) else ("BEGIN IMMEDIATE" if mode=="IMMEDIATE" else "BEGIN"))


def db_timestamp(value):
    return datetime.fromisoformat(value.replace("Z","+00:00")) if is_postgres() and isinstance(value,str) else value


def constraint_errors():
    if psycopg2:
        return (sqlite3.IntegrityError,psycopg2.IntegrityError)
    return (sqlite3.IntegrityError,)


@contextmanager
def transaction(conn):
    set_transaction_mode(conn,"IMMEDIATE")
    if is_postgres(conn): conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def apply_schema_migrations(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS deployment_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    if is_postgres(conn):
        conn.execute("INSERT INTO deployment_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",("storage_backend","postgresql"))
    def migration(version: int,name: str,apply):
        if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?",(version,)).fetchone():
            return
        with transaction(conn):
            # Recheck inside the write transaction: concurrent workers may start together.
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?",(version,)).fetchone():
                return
            apply()
            conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)",(version,name,now_iso()))
    def add_sales_customer_id():
        columns={r[1] for r in conn.execute("PRAGMA table_info(sales)").fetchall()}
        if "customer_id" not in columns:
            conn.execute("ALTER TABLE sales ADD COLUMN customer_id TEXT REFERENCES customers(id)")
    def add_product_metadata():
        columns={r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()}
        for column,sql in (("category","ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT ''"),("barcode","ALTER TABLE products ADD COLUMN barcode TEXT"),("min_stock_milli","ALTER TABLE products ADD COLUMN min_stock_milli INTEGER NOT NULL DEFAULT 0")):
            if column not in columns:
                conn.execute(sql)
    def add_barcode_index():
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS products_company_barcode ON products(company_id, barcode) WHERE barcode IS NOT NULL AND barcode <> ''")
    def backfill_paid_payables():
        conn.execute("""INSERT OR IGNORE INTO payable_payments(id,company_id,payable_id,amount_cents,payment_date,reference,idempotency_key,created_by,created_at)
            SELECT 'legacy-payment:'||ap.id,ap.company_id,ap.id,ap.amount_cents,substr(COALESCE(ap.paid_at,ap.created_at),1,10),
                   CASE WHEN length(trim(ap.payment_note))>=3 THEN trim(ap.payment_note) ELSE 'Baixa manual histórica registrada antes do ledger' END,
                   'legacy-paid:'||ap.id,ap.created_by,COALESCE(ap.paid_at,ap.created_at)
            FROM accounts_payable ap
            WHERE ap.status='paid' AND NOT EXISTS (SELECT 1 FROM payable_payments pp WHERE pp.company_id=ap.company_id AND pp.payable_id=ap.id)""")
    migration(1,"sales_customer_id",add_sales_customer_id)
    migration(2,"product_metadata",add_product_metadata)
    migration(3,"company_barcode_unique_index",add_barcode_index)
    migration(4,"backfill_legacy_paid_payables",backfill_paid_payables)
    def verify_tables(*names):
        def check():
            for name in names:
                if not conn.execute(f"PRAGMA table_info({name})").fetchone():
                    raise RuntimeError("Schema obrigatório ausente: "+name)
        return check
    migration(5,"plans_branches_entitlements_permissions",verify_tables("plans","plan_entitlements","company_subscriptions","company_module_entitlements","branches","branch_users","role_permissions","user_permissions"))
    migration(6,"restaurant_menu_tabs_orders_cash_payments",verify_tables("restaurant_menu_categories","restaurant_menu_items","restaurant_tabs","restaurant_orders","restaurant_order_items","restaurant_tab_payments","restaurant_events"))
    def add_nullable_branch_scope():
        # Historical records remain NULL (unassigned) until an explicit audited
        # reconciliation assigns them. Never silently attach legacy records to a branch.
        tables=("products","customers","suppliers","purchase_orders","purchase_order_items","purchase_receipts","purchase_receipt_items","accounts_payable","payable_payments","payable_expense_details","cash_registers","sales","sale_items","stock_movements","cash_movements","audit_events","restaurant_areas","restaurant_tables","restaurant_reservations","restaurant_services","restaurant_menu_categories","restaurant_menu_items","restaurant_tabs","restaurant_orders","restaurant_order_items","restaurant_tab_payments","restaurant_events")
        for table in tables:
            columns={r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "branch_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN branch_id TEXT REFERENCES branches(id)")
        # Explicit state documents that NULL data is not yet usable as partitioned
        # multi-branch operational data; no destructive update/backfill is done.
        conn.execute("INSERT INTO deployment_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",("branch_partitioning","schema_added_legacy_unassigned"))
    migration(7,"nullable_branch_scope_no_legacy_backfill",add_nullable_branch_scope)
    def create_branch_stock_ledger():
        conn.execute("""CREATE TABLE IF NOT EXISTS branch_product_stock(
            company_id TEXT NOT NULL REFERENCES companies(id),branch_id TEXT NOT NULL REFERENCES branches(id),
            product_id TEXT NOT NULL REFERENCES products(id),quantity_milli INTEGER NOT NULL CHECK(quantity_milli>=0),
            updated_at TEXT NOT NULL,PRIMARY KEY(company_id,branch_id,product_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS branch_stock_events(
            id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),branch_id TEXT NOT NULL REFERENCES branches(id),
            product_id TEXT NOT NULL REFERENCES products(id),actor_id TEXT NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL CHECK(kind IN ('adjustment_in','adjustment_out','transfer_in','transfer_out','sale','reversal')),
            quantity_milli INTEGER NOT NULL,before_milli INTEGER NOT NULL,after_milli INTEGER NOT NULL,
            reason TEXT NOT NULL,transfer_id TEXT,created_at TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS branch_stock_events_lookup ON branch_stock_events(company_id,branch_id,product_id,created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS branch_stock_transfers(
            id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),product_id TEXT NOT NULL REFERENCES products(id),
            from_branch_id TEXT NOT NULL REFERENCES branches(id),to_branch_id TEXT NOT NULL REFERENCES branches(id),
            quantity_milli INTEGER NOT NULL CHECK(quantity_milli>0),reason TEXT NOT NULL,actor_id TEXT NOT NULL REFERENCES users(id),
            idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(company_id,idempotency_key))""")
    migration(8,"branch_scoped_stock_ledger",create_branch_stock_ledger)
    def create_branch_cash_ledger():
        conn.execute("""CREATE TABLE IF NOT EXISTS branch_cash_registers(
            id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),branch_id TEXT NOT NULL REFERENCES branches(id),
            label TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('open','closed')),opening_cents INTEGER NOT NULL CHECK(opening_cents>=0),
            opened_by TEXT NOT NULL REFERENCES users(id),opened_at TEXT NOT NULL,closed_by TEXT REFERENCES users(id),closed_at TEXT,
            counted_cents INTEGER,expected_cents INTEGER,difference_cents INTEGER)""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS branch_one_open_register ON branch_cash_registers(company_id,branch_id) WHERE status='open'")
        conn.execute("""CREATE TABLE IF NOT EXISTS branch_sales(
            id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),branch_id TEXT NOT NULL REFERENCES branches(id),
            register_id TEXT NOT NULL REFERENCES branch_cash_registers(id),created_by TEXT NOT NULL REFERENCES users(id),
            idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('paid','voided')),
            subtotal_cents INTEGER NOT NULL,total_cents INTEGER NOT NULL,payment_method TEXT NOT NULL CHECK(payment_method='cash'),
            received_cents INTEGER NOT NULL,change_cents INTEGER NOT NULL,created_at TEXT NOT NULL,customer_id TEXT,
            UNIQUE(company_id,branch_id,idempotency_key))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS branch_sale_items(
            id TEXT PRIMARY KEY,company_id TEXT NOT NULL,branch_id TEXT NOT NULL,sale_id TEXT NOT NULL REFERENCES branch_sales(id),
            product_id TEXT NOT NULL REFERENCES products(id),sku TEXT NOT NULL,name TEXT NOT NULL,quantity_milli INTEGER NOT NULL,
            unit_price_cents INTEGER NOT NULL,discount_cents INTEGER NOT NULL,line_total_cents INTEGER NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS branch_cash_movements(
            id TEXT PRIMARY KEY,company_id TEXT NOT NULL,branch_id TEXT NOT NULL,register_id TEXT NOT NULL REFERENCES branch_cash_registers(id),
            actor_id TEXT NOT NULL REFERENCES users(id),kind TEXT NOT NULL CHECK(kind IN ('sale','supply','withdrawal')),
            amount_cents INTEGER NOT NULL,reason TEXT NOT NULL,sale_id TEXT,created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS branch_sales_returns(
            id TEXT PRIMARY KEY,company_id TEXT NOT NULL,branch_id TEXT NOT NULL,sale_id TEXT NOT NULL REFERENCES branch_sales(id),
            actor_id TEXT NOT NULL,amount_cents INTEGER NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS branch_cash_events(
            id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),branch_id TEXT NOT NULL REFERENCES branches(id),
            register_id TEXT NOT NULL REFERENCES branch_cash_registers(id),actor_id TEXT NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL CHECK(kind IN ('supply','withdrawal')),amount_cents INTEGER NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL)""")
    migration(9,"branch_scoped_cash_registers",create_branch_cash_ledger)
    def index_branch_purchases():
        conn.execute("CREATE INDEX IF NOT EXISTS purchase_orders_branch_company_date ON purchase_orders(company_id,branch_id,created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS purchase_receipts_branch_company_date ON purchase_receipts(company_id,branch_id,received_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS payables_branch_company_due ON accounts_payable(company_id,branch_id,status,due_date)")
    migration(10,"branch_purchase_scope_indexes",index_branch_purchases)
    def allow_branch_sale_reversals():
        if is_postgres(conn):
            constraints=conn.execute("SELECT conname FROM pg_constraint WHERE conrelid='branch_stock_events'::regclass AND contype='c' AND pg_get_constraintdef(oid) ILIKE ?",("%kind%",)).fetchall()
            for row in constraints:
                name=row[0]
                conn.execute(f'ALTER TABLE branch_stock_events DROP CONSTRAINT "{name.replace(chr(34),chr(34)*2)}"')
            conn.execute("ALTER TABLE branch_stock_events ADD CONSTRAINT branch_stock_events_kind_allowed CHECK(kind IN ('adjustment_in','adjustment_out','transfer_in','transfer_out','sale','reversal'))")
        else:
            # SQLite cannot alter CHECK; replace the append-only event table without
            # changing any historical rows and restore its lookup index.
            conn.execute("DROP INDEX IF EXISTS branch_stock_events_lookup")
            conn.execute("ALTER TABLE branch_stock_events RENAME TO branch_stock_events_old")
            conn.execute("""CREATE TABLE branch_stock_events(
                id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),branch_id TEXT NOT NULL REFERENCES branches(id),
                product_id TEXT NOT NULL REFERENCES products(id),actor_id TEXT NOT NULL REFERENCES users(id),
                kind TEXT NOT NULL CHECK(kind IN ('adjustment_in','adjustment_out','transfer_in','transfer_out','sale','reversal')),
                quantity_milli INTEGER NOT NULL,before_milli INTEGER NOT NULL,after_milli INTEGER NOT NULL,
                reason TEXT NOT NULL,transfer_id TEXT,created_at TEXT NOT NULL)""")
            conn.execute("INSERT INTO branch_stock_events SELECT * FROM branch_stock_events_old")
            conn.execute("DROP TABLE branch_stock_events_old")
            conn.execute("CREATE INDEX branch_stock_events_lookup ON branch_stock_events(company_id,branch_id,product_id,created_at)")
    migration(11,"branch_stock_sale_reversal_event_kinds",allow_branch_sale_reversals)
    def add_nullable_restaurant_branch_scope():
        tables=("restaurant_areas","restaurant_tables","restaurant_reservations","restaurant_services","restaurant_tabs","restaurant_orders","restaurant_order_items","restaurant_tab_payments","restaurant_events")
        for table in tables:
            if is_postgres(conn):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS branch_id TEXT REFERENCES branches(id)")
            else:
                cols={row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if "branch_id" not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN branch_id TEXT REFERENCES branches(id)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_branch_scope ON {table}(company_id,branch_id)")
    migration(12,"nullable_restaurant_branch_scope",add_nullable_restaurant_branch_scope)


def init_db() -> None:
    conn = connect_db()
    try:
        if not is_postgres(conn): conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
            email TEXT NOT NULL COLLATE NOCASE, full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('owner','admin','manager','operator','stock')),
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            UNIQUE(company_id, email)
        );
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
            sku TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'un',
            price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
            cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents >= 0),
            stock_milli INTEGER NOT NULL DEFAULT 0 CHECK(stock_milli >= 0),
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '', barcode TEXT, min_stock_milli INTEGER NOT NULL DEFAULT 0 CHECK(min_stock_milli >= 0),
            UNIQUE(company_id, sku)
        );
        CREATE TABLE IF NOT EXISTS customers (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), name TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', tax_id TEXT,
            notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS customers_company_name ON customers(company_id,active,name);
        CREATE UNIQUE INDEX IF NOT EXISTS customers_company_email ON customers(company_id,email) WHERE email <> '';
        CREATE TABLE IF NOT EXISTS suppliers (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), name TEXT NOT NULL,
            tax_id TEXT, email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(company_id,name)
        );
        CREATE TABLE IF NOT EXISTS purchase_orders (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), supplier_id TEXT NOT NULL REFERENCES suppliers(id),
            order_number TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','ordered','partially_received','received','cancelled')),
            notes TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(company_id,order_number)
        );
        CREATE TABLE IF NOT EXISTS purchase_order_items (
            id TEXT PRIMARY KEY, purchase_order_id TEXT NOT NULL REFERENCES purchase_orders(id),
            company_id TEXT NOT NULL REFERENCES companies(id), product_id TEXT NOT NULL REFERENCES products(id),
            ordered_milli INTEGER NOT NULL CHECK(ordered_milli>0), received_milli INTEGER NOT NULL DEFAULT 0 CHECK(received_milli>=0 AND received_milli<=ordered_milli),
            unit_cost_cents INTEGER NOT NULL CHECK(unit_cost_cents>=0)
        );
        CREATE TABLE IF NOT EXISTS purchase_receipts (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), purchase_order_id TEXT NOT NULL REFERENCES purchase_orders(id),
            received_by TEXT NOT NULL REFERENCES users(id), received_at TEXT NOT NULL, idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL, UNIQUE(company_id,idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS accounts_payable (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), supplier_id TEXT REFERENCES suppliers(id),
            source_receipt_id TEXT REFERENCES purchase_receipts(id), description TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents>=0), due_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('open','paid','cancelled')), created_by TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL, paid_at TEXT, payment_note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS payable_company_due ON accounts_payable(company_id,status,due_date);
        CREATE TABLE IF NOT EXISTS payable_payments (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
            payable_id TEXT NOT NULL REFERENCES accounts_payable(id), amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            payment_date TEXT NOT NULL, reference TEXT NOT NULL, idempotency_key TEXT NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
            UNIQUE(company_id,idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS payable_payments_parent ON payable_payments(company_id,payable_id);
        CREATE TABLE IF NOT EXISTS payable_expense_details (
            payable_id TEXT PRIMARY KEY REFERENCES accounts_payable(id), company_id TEXT NOT NULL REFERENCES companies(id),
            cost_center TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL DEFAULT '', request_hash TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS expense_idempotency
            ON payable_expense_details(company_id,idempotency_key) WHERE idempotency_key<>'';
        CREATE TRIGGER IF NOT EXISTS payable_payments_no_update BEFORE UPDATE ON payable_payments
            BEGIN SELECT RAISE(ABORT, 'payable payments are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS payable_payments_no_delete BEFORE DELETE ON payable_payments
            BEGIN SELECT RAISE(ABORT, 'payable payments are append-only'); END;
        CREATE TABLE IF NOT EXISTS purchase_receipt_items (
            id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL REFERENCES purchase_receipts(id),
            purchase_order_item_id TEXT NOT NULL REFERENCES purchase_order_items(id), product_id TEXT NOT NULL REFERENCES products(id),
            quantity_milli INTEGER NOT NULL CHECK(quantity_milli>0), unit_cost_cents INTEGER NOT NULL CHECK(unit_cost_cents>=0)
        );
        CREATE INDEX IF NOT EXISTS purchase_orders_company_date ON purchase_orders(company_id,created_at);
        CREATE TABLE IF NOT EXISTS cash_registers (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
            label TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','closed')),
            opening_cents INTEGER NOT NULL CHECK(opening_cents >= 0),
            opened_by TEXT NOT NULL REFERENCES users(id), opened_at TEXT NOT NULL,
            closed_by TEXT REFERENCES users(id), closed_at TEXT,
            counted_cents INTEGER, expected_cents INTEGER, difference_cents INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_register_per_company
            ON cash_registers(company_id) WHERE status = 'open';
        CREATE TABLE IF NOT EXISTS sales (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
            register_id TEXT NOT NULL REFERENCES cash_registers(id),
            created_by TEXT NOT NULL REFERENCES users(id), idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('paid','voided')),
            subtotal_cents INTEGER NOT NULL, total_cents INTEGER NOT NULL,
            payment_method TEXT NOT NULL CHECK(payment_method = 'cash'),
            received_cents INTEGER NOT NULL, change_cents INTEGER NOT NULL,
            created_at TEXT NOT NULL, customer_id TEXT REFERENCES customers(id), UNIQUE(company_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS sale_items (
            id TEXT PRIMARY KEY, sale_id TEXT NOT NULL REFERENCES sales(id),
            product_id TEXT NOT NULL REFERENCES products(id), sku TEXT NOT NULL,
            name TEXT NOT NULL, quantity_milli INTEGER NOT NULL CHECK(quantity_milli > 0),
            unit_price_cents INTEGER NOT NULL, discount_cents INTEGER NOT NULL,
            line_total_cents INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stock_movements (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
            product_id TEXT NOT NULL REFERENCES products(id), actor_id TEXT NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL CHECK(kind IN ('adjustment_in','adjustment_out','sale','reversal')),
            quantity_milli INTEGER NOT NULL, before_milli INTEGER NOT NULL, after_milli INTEGER NOT NULL,
            reason TEXT NOT NULL, sale_id TEXT REFERENCES sales(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cash_movements (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
            register_id TEXT NOT NULL REFERENCES cash_registers(id), actor_id TEXT NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL CHECK(kind IN ('sale','supply','withdrawal')),
            amount_cents INTEGER NOT NULL, reason TEXT NOT NULL, sale_id TEXT REFERENCES sales(id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
            actor_id TEXT REFERENCES users(id), action TEXT NOT NULL, entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS segment_catalog (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, questions_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS feature_catalog (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL,
            recommended_segments_json TEXT NOT NULL, dependencies_json TEXT NOT NULL, available INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS company_profiles (
            company_id TEXT PRIMARY KEY REFERENCES companies(id), primary_segment TEXT NOT NULL,
            secondary_segments_json TEXT NOT NULL, answers_json TEXT NOT NULL, modules_json TEXT NOT NULL,
            updated_by TEXT NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS branches (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), code TEXT NOT NULL, name TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','inactive')), created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
            UNIQUE(company_id,code)
        );
        CREATE TABLE IF NOT EXISTS branch_users (
            company_id TEXT NOT NULL REFERENCES companies(id), branch_id TEXT NOT NULL REFERENCES branches(id), user_id TEXT NOT NULL REFERENCES users(id),
            PRIMARY KEY(company_id,branch_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','active','retired')),
            price_cents INTEGER, billing_period TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plan_entitlements (
            plan_id TEXT NOT NULL REFERENCES plans(id), module_id TEXT NOT NULL, included INTEGER NOT NULL CHECK(included IN (0,1)),
            PRIMARY KEY(plan_id,module_id)
        );
        CREATE TABLE IF NOT EXISTS company_subscriptions (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), plan_id TEXT NOT NULL REFERENCES plans(id),
            status TEXT NOT NULL CHECK(status IN ('pending','active','past_due','suspended','cancelled')), starts_at TEXT, ends_at TEXT,
            created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS company_module_entitlements (
            company_id TEXT NOT NULL REFERENCES companies(id), module_id TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('plan','addon','admin')), status TEXT NOT NULL CHECK(status IN ('granted','revoked')),
            granted_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, PRIMARY KEY(company_id,module_id,source)
        );
        CREATE TABLE IF NOT EXISTS company_module_activations (
            company_id TEXT NOT NULL REFERENCES companies(id), module_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ENABLED','DISABLED','SUSPENDED')), activated_by TEXT NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL,
            PRIMARY KEY(company_id,module_id)
        );
        CREATE TABLE IF NOT EXISTS role_permissions (
            company_id TEXT NOT NULL REFERENCES companies(id), role TEXT NOT NULL, permission TEXT NOT NULL,
            allowed INTEGER NOT NULL CHECK(allowed IN (0,1)), updated_by TEXT NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL,
            PRIMARY KEY(company_id,role,permission)
        );
        CREATE TABLE IF NOT EXISTS user_permissions (
            company_id TEXT NOT NULL REFERENCES companies(id), user_id TEXT NOT NULL REFERENCES users(id), permission TEXT NOT NULL,
            allowed INTEGER NOT NULL CHECK(allowed IN (0,1)), updated_by TEXT NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL,
            PRIMARY KEY(company_id,user_id,permission)
        );
        CREATE TABLE IF NOT EXISTS module_events (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), module_id TEXT NOT NULL, status TEXT NOT NULL,
            actor_id TEXT NOT NULL REFERENCES users(id), details_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS restaurant_areas (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, UNIQUE(company_id,name)
        );
        CREATE TABLE IF NOT EXISTS restaurant_tables (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), area_id TEXT NOT NULL REFERENCES restaurant_areas(id),
            label TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity BETWEEN 1 AND 100),
            status TEXT NOT NULL CHECK(status IN ('available','occupied','reserved','awaiting_payment')),
            active INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, UNIQUE(company_id,label)
        );
        CREATE TABLE IF NOT EXISTS restaurant_reservations (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), table_id TEXT NOT NULL REFERENCES restaurant_tables(id),
            customer_name TEXT NOT NULL, party_size INTEGER NOT NULL CHECK(party_size BETWEEN 1 AND 100), starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('confirmed','seated','cancelled','completed')),
            created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, CHECK(ends_at>starts_at)
        );
        CREATE INDEX IF NOT EXISTS restaurant_reservation_overlap ON restaurant_reservations(company_id,table_id,status,starts_at,ends_at);
        CREATE TABLE IF NOT EXISTS restaurant_services (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), table_id TEXT NOT NULL REFERENCES restaurant_tables(id),
            status TEXT NOT NULL CHECK(status IN ('open','awaiting_payment','closed','cancelled')),
            opened_by TEXT NOT NULL REFERENCES users(id), opened_at TEXT NOT NULL, closed_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS restaurant_one_open_service_per_table ON restaurant_services(company_id,table_id) WHERE status='open';
        CREATE TABLE IF NOT EXISTS restaurant_menu_categories (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, UNIQUE(company_id,name)
        );
        CREATE TABLE IF NOT EXISTS restaurant_menu_items (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), category_id TEXT NOT NULL REFERENCES restaurant_menu_categories(id),
            product_id TEXT NOT NULL REFERENCES products(id), station TEXT NOT NULL CHECK(station IN ('kitchen','bar','counter')),
            active INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
            UNIQUE(company_id,product_id)
        );
        CREATE TABLE IF NOT EXISTS restaurant_tabs (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), service_id TEXT NOT NULL REFERENCES restaurant_services(id),
            label TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','closed','cancelled')),
            opened_by TEXT NOT NULL REFERENCES users(id), opened_at TEXT NOT NULL, closed_at TEXT,
            UNIQUE(company_id,label), UNIQUE(company_id,service_id,label)
        );
        CREATE TABLE IF NOT EXISTS restaurant_orders (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), tab_id TEXT NOT NULL REFERENCES restaurant_tabs(id),
            status TEXT NOT NULL CHECK(status IN ('open','sent','preparing','ready','delivered','cancelled')),
            notes TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS restaurant_order_items (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), order_id TEXT NOT NULL REFERENCES restaurant_orders(id),
            menu_item_id TEXT NOT NULL REFERENCES restaurant_menu_items(id), product_id TEXT NOT NULL REFERENCES products(id),
            name_snapshot TEXT NOT NULL, quantity_milli INTEGER NOT NULL CHECK(quantity_milli>0), unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents>=0),
            station TEXT NOT NULL CHECK(station IN ('kitchen','bar','counter')), status TEXT NOT NULL CHECK(status IN ('queued','preparing','ready','delivered','cancelled')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS restaurant_tab_payments (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), tab_id TEXT NOT NULL REFERENCES restaurant_tabs(id),
            amount_cents INTEGER NOT NULL CHECK(amount_cents>0), method TEXT NOT NULL CHECK(method='cash'), idempotency_key TEXT NOT NULL,
            received_by TEXT NOT NULL REFERENCES users(id), received_at TEXT NOT NULL, UNIQUE(company_id,idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS restaurant_events (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), tab_id TEXT NOT NULL REFERENCES restaurant_tabs(id),
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, action TEXT NOT NULL, details_json TEXT NOT NULL,
            actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS company_profile_versions (
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
            profile_json TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS company_profile_versions_history
            ON company_profile_versions(company_id, created_at);
        CREATE TRIGGER IF NOT EXISTS profile_versions_no_update BEFORE UPDATE ON company_profile_versions
            BEGIN SELECT RAISE(ABORT, 'profile versions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS profile_versions_no_delete BEFORE DELETE ON company_profile_versions
            BEGIN SELECT RAISE(ABORT, 'profile versions are append-only'); END;
        CREATE INDEX IF NOT EXISTS products_company_active ON products(company_id, active, name);
        CREATE INDEX IF NOT EXISTS sales_company_created ON sales(company_id, created_at);
        CREATE INDEX IF NOT EXISTS audit_company_created ON audit_events(company_id, created_at);
        CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
            BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
            BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS restaurant_events_no_update BEFORE UPDATE ON restaurant_events
            BEGIN SELECT RAISE(ABORT, 'restaurant events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS restaurant_events_no_delete BEFORE DELETE ON restaurant_events
            BEGIN SELECT RAISE(ABORT, 'restaurant events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS restaurant_payments_no_update BEFORE UPDATE ON restaurant_tab_payments
            BEGIN SELECT RAISE(ABORT, 'restaurant payments are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS restaurant_payments_no_delete BEFORE DELETE ON restaurant_tab_payments
            BEGIN SELECT RAISE(ABORT, 'restaurant payments are append-only'); END;
        """)
        apply_schema_migrations(conn)
        install_postgres_immutability(conn)
        for segment_id, segment in SEGMENT_CATALOG.items():
            conn.execute("INSERT OR REPLACE INTO segment_catalog(id,name,questions_json,active) VALUES(?,?,?,1)",
                         (segment_id, segment["name"], json.dumps(segment["questions"], ensure_ascii=False)))
        for feature_id, name, description, category, recommended, dependencies in FEATURES:
            conn.execute("INSERT OR REPLACE INTO feature_catalog(id,name,description,category,recommended_segments_json,dependencies_json,available) VALUES(?,?,?,?,?,?,?)",
                         (feature_id, name, description, category, json.dumps(recommended), json.dumps(dependencies),
                          int(feature_id in AVAILABLE_FEATURES)))
    finally:
        conn.close()


def audit(conn: sqlite3.Connection, company_id: str, actor_id: str | None,
          action: str, entity_type: str, entity_id: str, details: dict) -> None:
    conn.execute("INSERT INTO audit_events(id,company_id,actor_id,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                 (new_id(), company_id, actor_id, action, entity_type, entity_id,
                  json.dumps(details, ensure_ascii=False, sort_keys=True), now_iso()))


def seed_demo_data() -> None:
    """Create a fictional, disposable demo tenant. Never mix it with customer data."""
    conn = connect_db()
    try:
        with transaction(conn):
            created = now_iso()
            conn.execute("INSERT OR IGNORE INTO companies(id,name,slug,status,created_at) VALUES(?,?,?,'active',?)",
                         (DEMO_COMPANY_ID, "Mercado Modelo — Demonstração", "caixa-demo", created))
            conn.execute("INSERT OR IGNORE INTO users(id,company_id,email,full_name,password_hash,role,active,created_at) VALUES(?,?,?,?,?,'owner',1,?)",
                         (DEMO_OWNER_ID, DEMO_COMPANY_ID, "demo@caixa.local", "Conta de Demonstração",
                          password_hash("demo-somente-dados-ficticios"), created))
            examples = [
                ("00000000-0000-4000-8000-000000000011", "BEB-001", "Água mineral 500 ml", "un", 350, 180, 24_000),
                ("00000000-0000-4000-8000-000000000012", "MER-002", "Café torrado 250 g", "un", 1290, 790, 12_000),
                ("00000000-0000-4000-8000-000000000013", "LIM-003", "Detergente neutro 500 ml", "un", 275, 150, 18_000),
            ]
            for product_id, sku, name, unit, price, cost, stock in examples:
                exists = conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone()
                if not exists:
                    conn.execute("INSERT INTO products(id,company_id,sku,name,unit,price_cents,cost_cents,stock_milli,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (product_id, DEMO_COMPANY_ID, sku, name, unit, price, cost, stock, created))
                    conn.execute("INSERT INTO stock_movements(id,company_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,sale_id,created_at) VALUES(?,?,?,?,'adjustment_in',?,0,?,?,NULL,?)",
                                 (new_id(), DEMO_COMPANY_ID, product_id, DEMO_OWNER_ID, stock, stock,
                                  "Saldo inicial de demonstração — dados fictícios", created))
    finally:
        conn.close()


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return "pbkdf2_sha256$310000${}${}".format(
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="))


def password_matches(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_s, digest_s = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_s + "=" * (-len(salt_s) % 4))
        expected = base64.urlsafe_b64decode(digest_s + "=" * (-len(digest_s) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def create_token(user: sqlite3.Row) -> str:
    payload = {"sub": user["id"], "company_id": user["company_id"], "role": user["role"],
               "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    body = b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = b64url(hmac.new(TOKEN_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return body + "." + sig


def verify_token(token: str) -> dict:
    if not TOKEN_SECRET:
        raise HTTPException(503, "Autenticação indisponível: segredo do servidor não configurado.")
    try:
        body, signature = token.split(".", 1)
        expected = b64url(hmac.new(TOKEN_SECRET.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if int(payload["exp"]) <= int(time.time()):
            raise ValueError("expired")
        return payload
    except Exception:
        raise HTTPException(401, "Sessão inválida ou expirada.")


def decimal_milli(value: Decimal) -> int:
    return int((value * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def decimal_money_cents(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_money(cents: int) -> str:
    return str((Decimal(cents) / Decimal(100)).quantize(Decimal("0.01")))


def milli_to_decimal(milli: int) -> str:
    value = format(Decimal(milli) / Decimal(1000), "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BootstrapIn(StrictModel):
    company_name: str = Field(min_length=2, max_length=120)
    company_slug: str = Field(min_length=2, max_length=50)
    owner_name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=200)
    primary_segment: str | None = Field(default=None, min_length=2, max_length=40)
    secondary_segments: list[str] = Field(default_factory=list, max_length=5)
    answers: dict[str, bool] = Field(default_factory=dict)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Informe um e-mail válido")
        return value

    @field_validator("company_slug")
    @classmethod
    def normalize_slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("Use letras minúsculas, números e hífens no identificador")
        return value


class CompanyProfileIn(StrictModel):
    primary_segment: str = Field(min_length=2, max_length=40)
    secondary_segments: list[str] = Field(default_factory=list, max_length=5)
    answers: dict[str, bool] = Field(default_factory=dict)


class BranchCreateIn(StrictModel):
    code: str = Field(min_length=1,max_length=24,pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=2,max_length=120)

class BranchUsersIn(StrictModel):
    user_ids: list[str] = Field(max_length=500)

class BranchStockTransferIn(StrictModel):
    from_branch_id: str
    to_branch_id: str
    product_id: str
    quantity: Decimal = Field(gt=0, max_digits=15, decimal_places=3)
    reason: str = Field(min_length=3,max_length=240)

class BranchRegisterOpenIn(StrictModel):
    label: str = Field(min_length=1,max_length=60)
    opening_amount: Decimal = Field(ge=0,max_digits=12,decimal_places=2)

class BranchRegisterCloseIn(StrictModel):
    counted_amount: Decimal = Field(ge=0,max_digits=12,decimal_places=2)

class BranchCashMovementIn(StrictModel):
    kind: Literal["supply","withdrawal"]
    amount: Decimal = Field(gt=0,max_digits=12,decimal_places=2)
    reason: str = Field(min_length=3,max_length=240)

class BranchSaleLineIn(StrictModel):
    product_id: str
    quantity: Decimal = Field(gt=0,max_digits=15,decimal_places=3)
    discount: Decimal = Field(default=Decimal("0"),ge=0,max_digits=12,decimal_places=2)

class BranchSaleIn(StrictModel):
    items: list[BranchSaleLineIn] = Field(min_length=1,max_length=100)
    payment_method: Literal["cash"]
    received_amount: Decimal = Field(ge=0,max_digits=12,decimal_places=2)
    customer_id: str|None=None

class RestaurantAreaIn(StrictModel):
    name: str = Field(min_length=2,max_length=80)
class RestaurantTableIn(StrictModel):
    area_id: str
    label: str = Field(min_length=1,max_length=30)
    capacity: int = Field(ge=1,le=100)
class RestaurantReservationIn(StrictModel):
    table_id: str
    customer_name: str = Field(min_length=2,max_length=120)
    party_size: int = Field(ge=1,le=100)
    starts_at: str = Field(min_length=10,max_length=40)
    ends_at: str = Field(min_length=10,max_length=40)
class RestaurantCategoryIn(StrictModel):
    name: str = Field(min_length=2,max_length=80)
class RestaurantMenuItemIn(StrictModel):
    category_id: str
    product_id: str
    station: Literal["kitchen","bar","counter"] = "kitchen"
class RestaurantTabIn(StrictModel):
    label: str = Field(min_length=1,max_length=40)
class RestaurantOrderLineIn(StrictModel):
    menu_item_id: str
    quantity: Decimal = Field(gt=0,max_digits=12,decimal_places=3)
class RestaurantOrderIn(StrictModel):
    items: list[RestaurantOrderLineIn] = Field(min_length=1,max_length=50)
    notes: str = Field(default="",max_length=500)
class RestaurantItemStatusIn(StrictModel):
    status: Literal["preparing","ready","delivered","cancelled"]
class RestaurantCashPaymentIn(StrictModel):
    amount: Decimal = Field(gt=0,max_digits=12,decimal_places=2)
    received_amount: Decimal = Field(gt=0,max_digits=12,decimal_places=2)
    idempotency_key: str = Field(min_length=8,max_length=128)
class RolePermissionIn(StrictModel):
    role: Literal["admin","manager","operator","stock"]
    permission: str = Field(min_length=3,max_length=80)
    allowed: bool
class UserPermissionIn(StrictModel):
    permission: str = Field(min_length=3,max_length=80)
    allowed: bool
class PlanCreateIn(StrictModel):
    id: str = Field(min_length=2,max_length=40,pattern=r"^[a-z0-9_-]+$")
    name: str = Field(min_length=2,max_length=100)
    included_modules: list[str] = Field(default_factory=list,max_length=50)
    price: Decimal | None = Field(default=None,ge=0,max_digits=12,decimal_places=2)
    billing_period: Literal["monthly","annual"] | None = None
class SubscriptionAssignIn(StrictModel):
    plan_id: str
    status: Literal["pending"] = "pending"
    starts_at: str | None = None
    ends_at: str | None = None


class LoginIn(StrictModel):
    company_slug: str = Field(min_length=2, max_length=50)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)

    @field_validator("company_slug")
    @classmethod
    def normalize_login_slug(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("email")
    @classmethod
    def normalize_login_email(cls, value: str) -> str:
        return value.strip().lower()


class UserCreateIn(StrictModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=200)
    role: Literal["admin", "manager", "operator", "stock"]

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Informe um e-mail válido")
        return value


class UserStatusIn(StrictModel):
    active: bool


class ProductIn(StrictModel):
    sku: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=160)
    unit: str = Field(default="un", min_length=1, max_length=12)
    price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    cost: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    category: str = Field(default="", max_length=80)
    barcode: str | None = Field(default=None, min_length=4, max_length=64)
    minimum_stock: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=3)

    @field_validator("sku", "name", "unit", "category")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()


class CustomerIn(StrictModel):
    name: str = Field(min_length=2,max_length=160)
    email: str = Field(default="",max_length=254)
    phone: str = Field(default="",max_length=40)
    tax_id: str | None = Field(default=None,max_length=32)
    notes: str = Field(default="",max_length=500)

    @field_validator("name","email","phone","tax_id","notes")
    @classmethod
    def trim_customer_fields(cls,value):
        return value.strip().lower() if isinstance(value,str) and value and "@" in value else value.strip() if isinstance(value,str) else value

    @field_validator("email")
    @classmethod
    def validate_customer_email(cls,value):
        if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",value): raise ValueError("Informe um e-mail válido")
        return value


class SupplierIn(StrictModel):
    name: str = Field(min_length=2,max_length=160)
    tax_id: str | None = Field(default=None,max_length=32)
    email: str = Field(default="",max_length=254)
    phone: str = Field(default="",max_length=40)
    notes: str = Field(default="",max_length=500)

    @field_validator("name","tax_id","email","phone","notes")
    @classmethod
    def trim_supplier_fields(cls,value):
        return value.strip() if isinstance(value,str) else value


class PurchaseLineIn(StrictModel):
    product_id: str
    quantity: Decimal = Field(gt=0,max_digits=12,decimal_places=3)
    unit_cost: Decimal = Field(ge=0,max_digits=12,decimal_places=2)


class PurchaseCreateIn(StrictModel):
    supplier_id: str
    order_number: str = Field(min_length=1,max_length=60)
    notes: str = Field(default="",max_length=500)
    items: list[PurchaseLineIn] = Field(min_length=1,max_length=100)


class PurchaseStatusIn(StrictModel):
    status: Literal["ordered","cancelled"]


class ReceiveLineIn(StrictModel):
    order_item_id: str
    quantity: Decimal = Field(gt=0,max_digits=12,decimal_places=3)


class PurchaseReceiveIn(StrictModel):
    items: list[ReceiveLineIn] = Field(min_length=1,max_length=100)
    due_date: date | None = None


class PayablePaidIn(StrictModel):
    payment_note: str = Field(min_length=3,max_length=240)

    @field_validator("payment_note")
    @classmethod
    def trim_payment_note(cls,value):
        value=value.strip()
        if len(value)<3: raise ValueError("Informe uma referência ou justificativa")
        return value


class ExpenseIn(StrictModel):
    description: str = Field(min_length=2,max_length=240)
    amount: Decimal = Field(gt=0,max_digits=12,decimal_places=2)
    due_date: date
    cost_center: str = Field(default="",max_length=120)
    payment_date: date | None = None
    payment_reference: str = Field(default="",max_length=240)

    @field_validator("description","cost_center","payment_reference")
    @classmethod
    def trim_expense_fields(cls,value):
        return value.strip()

    @field_validator("payment_reference")
    @classmethod
    def require_expense_payment_reference(cls,value,info):
        if info.data.get("payment_date") and len(value)<3:
            raise ValueError("Informe referência/justificativa da baixa manual")
        return value


class PayablePartialIn(StrictModel):
    amount: Decimal = Field(gt=0,max_digits=12,decimal_places=2)
    payment_date: date
    reference: str = Field(min_length=3,max_length=240)

    @field_validator("reference")
    @classmethod
    def trim_partial_reference(cls,value):
        value=value.strip()
        if len(value)<3: raise ValueError("Informe referência/justificativa da baixa manual")
        return value


class StockAdjustmentIn(StrictModel):
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    direction: Literal["in", "out"]
    reason: str = Field(min_length=3, max_length=240)

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("Informe uma justificativa")
        return value


class RegisterOpenIn(StrictModel):
    label: str = Field(default="Caixa 01", min_length=1, max_length=60)
    opening_amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


class RegisterCloseIn(StrictModel):
    counted_amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


class CashMovementIn(StrictModel):
    kind: Literal["supply", "withdrawal"]
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reason: str = Field(min_length=3, max_length=240)


class SaleVoidIn(StrictModel):
    reason: str = Field(min_length=5,max_length=240)

    @field_validator("reason")
    @classmethod
    def trim_reason(cls,value):
        value=value.strip()
        if len(value)<5: raise ValueError("Informe o motivo do cancelamento")
        return value


class SaleLineIn(StrictModel):
    product_id: str
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    discount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)


class SaleIn(StrictModel):
    items: list[SaleLineIn] = Field(min_length=1, max_length=100)
    payment_method: Literal["cash"]
    received_amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    customer_id: str | None = None


bearer = HTTPBearer(auto_error=False)
@asynccontextmanager
async def lifespan(_app):
    for name, secret in (("CAIXA_TOKEN_SECRET", TOKEN_SECRET), ("CAIXA_BOOTSTRAP_SECRET", BOOTSTRAP_SECRET)):
        if not secret:
            raise RuntimeError(f"{name} precisa estar configurado no ambiente, nunca no código-fonte.")
        if len(secret.encode()) < 32:
            raise RuntimeError(f"{name} deve conter no mínimo 32 bytes. Use um segredo aleatório forte.")
    if REQUIRE_POSTGRES:
        require_persistent_database()
    init_db()
    if DEMO_MODE:
        seed_demo_data()
    yield


app = FastAPI(title="Caixa by VagasIO API", version=APP_VERSION,
              description="API inicial de desenvolvimento para varejo geral. Não integra adquirente nem emissor fiscal.", lifespan=lifespan)
_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
app.mount("/static", StaticFiles(directory=str(_FRONTEND)), name="static")
_origins = [x.strip() for x in os.environ.get("CAIXA_ALLOWED_ORIGINS", "").split(",") if x.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PATCH"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Bootstrap-Key"])


def db_dep():
    conn = connect_db()
    try:
        yield conn
    finally:
        conn.close()


DB = Annotated[sqlite3.Connection, Depends(db_dep)]


def current_user(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)], conn: DB) -> dict:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Autenticação necessária.", headers={"WWW-Authenticate": "Bearer"})
    claims = verify_token(credentials.credentials)
    row = conn.execute("""SELECT u.id,u.company_id,u.email,u.full_name,u.role,u.active,c.status AS company_status
        FROM users u JOIN companies c ON c.id=u.company_id WHERE u.id=? AND u.company_id=?""",
        (claims.get("sub"), claims.get("company_id"))).fetchone()
    if not row or not row["active"] or row["company_status"] != "active" or row["role"] != claims.get("role"):
        raise HTTPException(401, "Sessão inválida ou usuário inativo.")
    return dict(row)


USER = Annotated[dict, Depends(current_user)]


PERMISSION_CATALOG={"products.read":"Consultar produtos","products.write":"Gerenciar produtos","stock.adjust":"Ajustar estoque","sales.create":"Registrar vendas","sales.void":"Cancelar vendas","cash.manage":"Operar caixa","customers.read":"Consultar clientes","customers.write":"Gerenciar clientes","purchases.manage":"Gerenciar compras","finance.read":"Consultar financeiro","finance.write":"Gerenciar financeiro","users.manage":"Gerenciar acessos","profile.manage":"Configurar perfil","branches.manage":"Gerenciar filiais","modules.manage":"Gerenciar módulos","restaurant.floor":"Operar ambientes, mesas e reservas","reports.read":"Consultar relatórios","restaurant.menu":"Gerenciar cardápio","restaurant.orders":"Criar e distribuir pedidos","restaurant.production":"Atualizar produção e entrega","restaurant.cash":"Receber e encerrar comandas","sales.read":"Consultar vendas e relatórios","inventory.read":"Consultar estoque","audit.read":"Consultar auditoria","branch.read":"Consultar filiais","users.read":"Consultar equipe"}
ROLE_PERMISSIONS={"owner":set(PERMISSION_CATALOG),"admin":set(PERMISSION_CATALOG)-{"branches.manage","modules.manage"},"manager":set(PERMISSION_CATALOG)-{"users.manage","branches.manage","modules.manage"},"operator":{"products.read","products.write","sales.create","sales.read","sales.void","cash.manage","inventory.read","customers.read","customers.write","restaurant.floor","restaurant.orders","restaurant.production","restaurant.cash","reports.read","branch.read"},"stock":{"products.read","products.write","stock.adjust","purchases.manage","inventory.read","branch.read"}}
MODULE_STATUSES={"sales":"ENABLED","inventory":"ENABLED","purchasing":"ENABLED","customers":"ENABLED","finance":"ENABLED","reports":"ENABLED","staff":"ENABLED","profile_setup":"ENABLED","restaurant_floor":"AVAILABLE","barcode":"NOT_IMPLEMENTED","weighed_items":"NOT_IMPLEMENTED","expiry_control":"NOT_IMPLEMENTED","tables_tabs":"NOT_IMPLEMENTED","delivery":"NOT_IMPLEMENTED","kitchen":"NOT_IMPLEMENTED","variants":"NOT_IMPLEMENTED","returns":"NOT_IMPLEMENTED","commissions":"NOT_IMPLEMENTED","appointments":"NOT_IMPLEMENTED","measurement_sales":"NOT_IMPLEMENTED","restaurant_floor":"AVAILABLE"}

def require_role(user: dict, *roles: str) -> None:
    if user["role"] not in roles:
        raise HTTPException(403, "Seu perfil não permite esta operação.")


def require_permission(conn,user,permission):
    if permission not in PERMISSION_CATALOG: raise HTTPException(500,"Permissão não cadastrada.")
    override=conn.execute("SELECT allowed FROM user_permissions WHERE company_id=? AND user_id=? AND permission=?",(user["company_id"],user["id"],permission)).fetchone()
    if override is not None:
        if not override[0]: raise HTTPException(403,"Seu acesso não permite esta ação.")
        return
    role=conn.execute("SELECT allowed FROM role_permissions WHERE company_id=? AND role=? AND permission=?",(user["company_id"],user["role"],permission)).fetchone()
    allowed=bool(role[0]) if role is not None else permission in ROLE_PERMISSIONS.get(user["role"],set())
    if not allowed: raise HTTPException(403,"Seu perfil não permite esta ação.")


def company_product(conn: sqlite3.Connection, company_id: str, product_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM products WHERE id=? AND company_id=? AND active=1", (product_id, company_id)).fetchone()
    if not row:
        raise HTTPException(404, "Produto não encontrado.")
    return row


def product_json(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "sku": row["sku"], "name": row["name"], "unit": row["unit"],
            "price": cents_to_money(row["price_cents"]), "cost": cents_to_money(row["cost_cents"]),
            "stock": milli_to_decimal(row["stock_milli"]), "category": row["category"], "barcode": row["barcode"],
            "minimum_stock": milli_to_decimal(row["min_stock_milli"]), "low_stock": row["min_stock_milli"] > 0 and row["stock_milli"] <= row["min_stock_milli"],
            "active": bool(row["active"]), "created_at": row["created_at"]}


def register_json(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "label": row["label"], "status": row["status"],
            "opening_amount": cents_to_money(row["opening_cents"]), "opened_at": row["opened_at"],
            "closed_at": row["closed_at"],
            "expected_amount": cents_to_money(row["expected_cents"]) if row["expected_cents"] is not None else None,
            "counted_amount": cents_to_money(row["counted_cents"]) if row["counted_cents"] is not None else None,
            "difference": cents_to_money(row["difference_cents"]) if row["difference_cents"] is not None else None}


def sale_json(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    items = conn.execute("SELECT sku,name,quantity_milli,unit_price_cents,discount_cents,line_total_cents FROM sale_items WHERE sale_id=?", (row["id"],)).fetchall()
    return {"id": row["id"], "status": row["status"], "register_id": row["register_id"],
            "customer_id": row["customer_id"],
            "subtotal": cents_to_money(row["subtotal_cents"]), "total": cents_to_money(row["total_cents"]),
            "payment_method": row["payment_method"], "received_amount": cents_to_money(row["received_cents"]),
            "change": cents_to_money(row["change_cents"]), "created_at": row["created_at"],
            "items": [{"sku": i["sku"], "name": i["name"], "quantity": milli_to_decimal(i["quantity_milli"]),
                       "unit_price": cents_to_money(i["unit_price_cents"]), "discount": cents_to_money(i["discount_cents"]),
                       "line_total": cents_to_money(i["line_total_cents"])} for i in items]}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/v1/") else "no-cache"
    return response


def permission_for_endpoint(method: str, path: str):
    method=method.upper()
    if path in {"/v1/auth/login","/v1/auth/demo","/v1/companies/register","/v1/bootstrap","/v1/segments"}: return None
    if path=="/v1/me": return None
    if path.startswith("/v1/master/"): return None  # separately protected by bootstrap secret
    if path.startswith("/v1/company/branches"):
        return "branches.manage" if "/users" in path or method!="GET" else "branch.read"
    if path=="/v1/branches/stock-transfers": return "stock.adjust"
    if path.startswith("/v1/branches/") and "/registers" in path: return "cash.manage"
    if path.startswith("/v1/branches/") and "/purchase-orders" in path: return "purchases.manage"
    if path.startswith("/v1/branches/") and "/payables" in path: return "finance.write" if method not in {"GET","HEAD"} else "finance.read"
    if path.startswith("/v1/branches/") and "/finance" in path: return "finance.write" if method not in {"GET","HEAD"} else "finance.read"
    if path.startswith("/v1/branches/") and ("/dashboard/" in path or "/reports/" in path): return "reports.read"
    if path.startswith("/v1/branches/") and "/sales" in path: return "sales.void" if path.endswith("/void") else "sales.read" if method=="GET" else "sales.create"
    if path.startswith("/v1/branches/"):
        return "stock.adjust" if method!="GET" else "inventory.read"
    if path=="/v1/company/commercial": return "finance.read"
    if path=="/v1/permissions/effective": return None
    if path.startswith("/v1/company/permissions") or path.startswith("/v1/users/") and path.endswith("/permissions"):
        return "users.manage"
    if path.startswith("/v1/company/modules") or path.startswith("/v1/company/profile"):
        return "modules.manage" if "/modules/" in path else "profile.manage"
    if path.startswith("/v1/restaurant/"):
        if "/menu" in path: return "restaurant.menu" if method!="GET" else "products.read"
        if "/payments" in path: return "restaurant.cash"
        if "/production" in path or "/order-items/" in path: return "restaurant.production"
        if "/reports/" in path: return "reports.read"
        if "/tabs" in path or "/orders" in path or "/services/" in path: return "restaurant.orders"
        return "restaurant.floor"
    if path.startswith("/v1/products"):
        if "stock-adjustments" in path: return "stock.adjust"
        if method in {"POST","PUT","PATCH","DELETE"}: return "products.write"
        return "products.read"
    if path.startswith("/v1/stock-movements"): return "inventory.read"
    if path.startswith("/v1/registers"):
        if method=="GET": return "cash.manage"
        return "cash.manage"
    if path.startswith("/v1/sales"):
        return "sales.void" if "/void" in path else "sales.create" if method=="POST" else "sales.read"
    if path.startswith("/v1/customers"):
        return "customers.write" if method in {"POST","PUT","PATCH","DELETE"} else "customers.read"
    if path.startswith("/v1/suppliers") or path.startswith("/v1/purchase-orders"): return "purchases.manage"
    if path.startswith("/v1/finance"):
        return "finance.write" if method not in {"GET","HEAD"} else "finance.read"
    if path.startswith("/v1/users"):
        return "users.read" if method=="GET" else "users.manage"
    if path.startswith("/v1/reports") or path.startswith("/v1/dashboard"): return "reports.read"
    if path.startswith("/v1/audit-events"): return "audit.read"
    if path.startswith("/v1/configuration/catalog"): return "profile.manage"
    return None


@app.middleware("http")
async def granular_permission_guard(request: Request, call_next):
    permission=permission_for_endpoint(request.method,request.url.path)
    if permission:
        auth=request.headers.get("authorization","")
        if auth.lower().startswith("bearer "):
            conn=connect_db()
            try:
                try:
                    claims=verify_token(auth.split(None,1)[1])
                    row=conn.execute("SELECT u.id,u.company_id,u.email,u.full_name,u.role,u.active,c.status AS company_status FROM users u JOIN companies c ON c.id=u.company_id WHERE u.id=? AND u.company_id=?",(claims.get("sub"),claims.get("company_id"))).fetchone()
                    if not row or not row["active"] or row["company_status"]!="active" or row["role"]!=claims.get("role"):
                        raise HTTPException(401,"Sessão inválida ou usuário inativo.")
                    require_permission(conn,dict(row),permission)
                    company_user=dict(row)
                    branch_exempt=(request.url.path in {"/v1/me","/v1/configuration/catalog","/v1/company/branches","/v1/company/commercial","/v1/permissions/effective","/v1/audit-events"} or request.url.path.startswith(("/v1/company/profile","/v1/company/modules","/v1/company/permissions","/v1/company/branches/","/v1/permissions/","/v1/users")))
                    if not branch_exempt and request.url.path.startswith("/v1/"):
                        active_branches=conn.execute("SELECT id FROM branches WHERE company_id=? AND status='active' ORDER BY created_at,id",(company_user["company_id"],)).fetchall()
                        requested_branch=request.headers.get("X-Branch-Id")
                        if requested_branch:
                            authorized=conn.execute("SELECT 1 FROM branch_users bu JOIN branches b ON b.id=bu.branch_id AND b.company_id=bu.company_id WHERE bu.company_id=? AND bu.user_id=? AND bu.branch_id=? AND b.status='active'",(company_user["company_id"],company_user["id"],requested_branch)).fetchone()
                            if not authorized: raise HTTPException(403,"Filial inexistente ou não autorizada para este usuário.")
                            catalog_write=(request.url.path=="/v1/products" and request.method=="POST") or (request.url.path.startswith("/v1/products/") and request.method=="PUT" and "stock-adjustments" not in request.url.path) or (request.url.path in {"/v1/suppliers","/v1/customers"} and request.method=="POST")
                            shared_read=request.method=="GET" and request.url.path in {"/v1/suppliers","/v1/customers"}
                            if len(active_branches)>1 and not request.url.path.startswith("/v1/branches/") and not shared_read and not catalog_write:
                                raise HTTPException(409,"Particionamento operacional por filial ainda não foi ativado; esta operação não altera nem filtra dados até os fluxos serem migrados.")
                            request.state.branch_id=requested_branch
                        elif len(active_branches)>1 and not request.url.path.startswith("/v1/branches/") and not (request.method=="GET" and request.url.path in {"/v1/suppliers","/v1/customers"}) and not ((request.url.path in {"/v1/products","/v1/suppliers","/v1/customers"} and request.method=="POST") or (request.url.path.startswith("/v1/products/") and request.method=="PUT" and "stock-adjustments" not in request.url.path)):
                            raise HTTPException(409,"Operações multi-filial ainda não estão particionadas. Os dados históricos permanecem sem atribuição e nenhuma filial foi selecionada automaticamente.")
                        elif request.url.path.startswith("/v1/branches/"):
                            match=re.match(r"^/v1/branches/([^/]+)(?:/|$)",request.url.path)
                            if match and match.group(1)!="stock-transfers":
                                authorized=conn.execute("SELECT 1 FROM branch_users bu JOIN branches b ON b.id=bu.branch_id AND b.company_id=bu.company_id WHERE bu.company_id=? AND bu.user_id=? AND bu.branch_id=? AND b.status='active'",(company_user["company_id"],company_user["id"],match.group(1))).fetchone()
                                if not authorized: raise HTTPException(403,"Filial inexistente ou não autorizada para este usuário.")
                        elif active_branches:
                            authorized=conn.execute("SELECT 1 FROM branch_users WHERE company_id=? AND user_id=? AND branch_id=?",(company_user["company_id"],company_user["id"],active_branches[0]["id"])).fetchone()
                            if not authorized: raise HTTPException(403,"Este usuário não tem acesso à filial principal.")
                except HTTPException as exc:
                    return JSONResponse(status_code=exc.status_code,content={"detail":exc.detail},headers=exc.headers)
            finally: conn.close()
    response=await call_next(request)
    return response


@app.get("/", include_in_schema=False)
def frontend_index():
    return FileResponse(_FRONTEND / "index.html")


@app.get("/health")
def health():
    backend="postgresql" if POSTGRES_BACKEND else "sqlite-ephemeral"
    return {"status": "ok", "service": "caixa-api", "version": APP_VERSION,
            "mode": "development-foundation", "storage": backend,
            "demo_enabled": DEMO_MODE,
            "signup_enabled": bool(POSTGRES_BACKEND and PUBLIC_SIGNUP and not DEMO_MODE),
            "integrations": {"payments": False, "fiscal": False}}


@app.post("/v1/auth/demo")
def demo_login(conn: DB):
    if not DEMO_MODE:
        raise HTTPException(404, "Modo de demonstração não está habilitado.")
    row = conn.execute("SELECT * FROM users WHERE id=? AND company_id=? AND active=1",
                       (DEMO_OWNER_ID, DEMO_COMPANY_ID)).fetchone()
    if not row:
        raise HTTPException(503, "Conta demonstrativa indisponível.")
    return {"access_token": create_token(row), "token_type": "bearer", "expires_in": TOKEN_TTL_SECONDS,
            "user": {"id": row["id"], "company_id": row["company_id"], "email": row["email"],
                     "name": row["full_name"], "role": row["role"]}, "demo": True}


def resolve_profile_modules(answers: dict[str, bool]) -> list[str]:
    """Only persist modules already implemented in this release; recommendations stay in answers."""
    return sorted(IMPLEMENTED_FEATURES-{"barcode"})


def validate_initial_profile(data: BootstrapIn, *, required: bool = False):
    if data.primary_segment is None:
        if required or data.secondary_segments or data.answers:
            raise HTTPException(422, "Selecione a atividade principal antes de responder às perguntas.")
        return None  # Existing administrative/bootstrap clients remain compatible.
    if data.primary_segment not in SEGMENT_CATALOG or any(x not in SEGMENT_CATALOG for x in data.secondary_segments):
        raise HTTPException(422, "Selecione atividades disponíveis.")
    secondary = list(dict.fromkeys(data.secondary_segments))
    if data.primary_segment in secondary:
        raise HTTPException(422, "A atividade principal não precisa ser repetida nas atividades secundárias.")
    allowed = {q["id"] for segment_id in [data.primary_segment, *secondary]
               for q in SEGMENT_CATALOG[segment_id]["questions"]}
    if set(data.answers) - allowed:
        raise HTTPException(422, "Revise as respostas para as atividades selecionadas.")
    return {"primary_segment": data.primary_segment, "secondary_segments": secondary,
            "answers": data.answers, "modules": resolve_profile_modules(data.answers)}


@app.get("/v1/segments")
def public_segments():
    return {"items": [{"id": sid, "name": item["name"], "questions": item["questions"]}
                      for sid, item in SEGMENT_CATALOG.items()]}


@app.post("/v1/companies/register", status_code=201)
def register_company(data: BootstrapIn, request: Request, conn: DB):
    """Self-service tenant creation on the durable PostgreSQL backend only."""
    if DEMO_MODE or not POSTGRES_BACKEND or not PUBLIC_SIGNUP:
        # Never create persistent customer tenants from the shared public demo.
        raise HTTPException(404, "Cadastro público temporariamente indisponível.")
    if data.primary_segment is None:
        raise HTTPException(422, "Selecione a atividade principal da empresa.")
    profile = validate_initial_profile(data, required=True)
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(f"signup:{client_ip}", 3, 3600)
    company_id, user_id, created = new_id(), new_id(), now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO companies VALUES(?,?,?,'active',?)",
                         (company_id, data.company_name.strip(), data.company_slug, created))
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,'owner',1,?)",
                         (user_id, company_id, data.email, data.owner_name.strip(), password_hash(data.password), created))
            branch_id=new_id()
            conn.execute("INSERT INTO branches(id,company_id,code,name,status,created_by,created_at) VALUES(?,?,'main','Matriz','active',?,?)",(branch_id,company_id,user_id,created))
            conn.execute("INSERT INTO branch_users(company_id,branch_id,user_id) VALUES(?,?,?)",(company_id,branch_id,user_id))
            audit(conn, company_id, user_id, "company.self_registered", "company", company_id,
                  {"company_slug": data.company_slug, "actor_email": data.email})
            if profile:
                snapshot = {**profile}
                conn.execute("INSERT INTO company_profile_versions VALUES(?,?,?,?,?)",
                             (new_id(), company_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), user_id, created))
                conn.execute("""INSERT INTO company_profiles(company_id,primary_segment,secondary_segments_json,answers_json,modules_json,updated_by,updated_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (company_id, profile["primary_segment"], json.dumps(profile["secondary_segments"], ensure_ascii=False),
                     json.dumps(profile["answers"], sort_keys=True), json.dumps(profile["modules"]), user_id, created))
                audit(conn, company_id, user_id, "company.profile_configured", "company_profile", company_id,
                      {"primary_segment": profile["primary_segment"], "secondary_segments": profile["secondary_segments"],
                       "answers": profile["answers"], "modules": profile["modules"], "source": "signup"})
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Este identificador de empresa ou e-mail já está em uso.")
    return {"company_id": company_id, "status": "created",
            "message": "Empresa criada. Entre com o identificador, e-mail e senha cadastrados."}


@app.post("/v1/bootstrap", status_code=201)
def bootstrap(data: BootstrapIn, request: Request, conn: DB,
              bootstrap_key: Annotated[str | None, Header(alias="X-Bootstrap-Key")] = None):
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(f"bootstrap:{client_ip}", 10, 3600)
    if not BOOTSTRAP_SECRET:
        raise HTTPException(503, "Provisionamento desabilitado: configure a chave de bootstrap fora do código.")
    if not bootstrap_key or not hmac.compare_digest(bootstrap_key, BOOTSTRAP_SECRET):
        raise HTTPException(403, "Chave de provisionamento inválida.")
    company_id, user_id, created = new_id(), new_id(), now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO companies VALUES(?,?,?,'active',?)", (company_id, data.company_name.strip(), data.company_slug, created))
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,'owner',1,?)",
                         (user_id, company_id, data.email, data.owner_name.strip(), password_hash(data.password), created))
            branch_id=new_id()
            conn.execute("INSERT INTO branches(id,company_id,code,name,status,created_by,created_at) VALUES(?,?,'main','Matriz','active',?,?)",(branch_id,company_id,user_id,created))
            conn.execute("INSERT INTO branch_users(company_id,branch_id,user_id) VALUES(?,?,?)",(company_id,branch_id,user_id))
            audit(conn, company_id, user_id, "company.bootstrap", "company", company_id,
                  {"company_slug": data.company_slug, "actor_email": data.email})
    except sqlite3.IntegrityError:
        raise HTTPException(409, "O identificador da empresa ou e-mail já está cadastrado.")
    return {"company_id": company_id, "owner_user_id": user_id, "status": "created",
            "next_step": "Faça login usando o identificador da empresa, e-mail e senha.", "session": None}


@app.post("/v1/auth/login")
def login(data: LoginIn, request: Request, conn: DB):
    email = data.email.strip().lower()
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(f"login-ip:{client_ip}", 30, 900)
    enforce_rate_limit(f"login-account:{data.company_slug}:{email}", 8, 900)
    row = conn.execute("""SELECT u.*,c.status AS company_status FROM users u JOIN companies c ON c.id=u.company_id
        WHERE c.slug=? AND u.email=?""", (data.company_slug, email)).fetchone()
    if not row:
        # Reduce timing differences that could reveal whether a tenant/account exists.
        password_matches(data.password, "pbkdf2_sha256$310000$Zml4ZWQtc2FsdA$ZJrv7qPNcYq3Ng8VDJXh8f7JXlHQE0jQhF6Y7joZaZg")
    if not row or not password_matches(data.password, row["password_hash"]) or not row["active"] or row["company_status"] != "active":
        raise HTTPException(401, "Empresa, e-mail ou senha inválidos.")
    return {"access_token": create_token(row), "token_type": "bearer", "expires_in": TOKEN_TTL_SECONDS,
            "user": {"id": row["id"], "company_id": row["company_id"], "email": row["email"],
                     "name": row["full_name"], "role": row["role"]}}


@app.get("/v1/configuration/catalog")
def configuration_catalog(conn: DB,user: USER):
    segments=conn.execute("SELECT * FROM segment_catalog WHERE active=1 ORDER BY name").fetchall()
    rows=conn.execute("SELECT * FROM feature_catalog ORDER BY category,name").fetchall()
    profile=conn.execute("SELECT * FROM company_profiles WHERE company_id=?",(user["company_id"],)).fetchone()
    selected=[profile["primary_segment"],*json.loads(profile["secondary_segments_json"])] if profile else []
    profile_modules=set(json.loads(profile["modules_json"])) if profile else IMPLEMENTED_FEATURES
    features=[]
    for r in rows:
        mid=r["id"];compatible="*" in json.loads(r["recommended_segments_json"]) or any(seg in json.loads(r["recommended_segments_json"]) for seg in selected)
        enabled=bool(r["available"] and mid in profile_modules and compatible)
        status="NOT_IMPLEMENTED" if not r["available"] else "ENABLED" if enabled else "DISABLED" if selected and not compatible else "REQUIRES_CONFIGURATION" if not selected else "AVAILABLE"
        features.append({"id":mid,"name":r["name"],"description":r["description"],"category":r["category"],"recommended_segments":json.loads(r["recommended_segments_json"]),"dependencies":json.loads(r["dependencies_json"]),"available":bool(r["available"]),"enabled":enabled,"status":status,"required_plan":None,"plan_eligible":None})
    return {"segments":[{"id":r["id"],"name":r["name"],"questions":json.loads(r["questions_json"])} for r in segments],"features":features,
      "profile":{"primary_segment":profile["primary_segment"],"secondary_segments":json.loads(profile["secondary_segments_json"]),"answers":json.loads(profile["answers_json"]),"modules":json.loads(profile["modules_json"]),"updated_at":profile["updated_at"]} if profile else None,
      "plan_entitlements":"configured" if conn.execute("SELECT 1 FROM company_subscriptions WHERE company_id=? LIMIT 1",(user["company_id"],)).fetchone() else "not_configured",
      "activation_note":"Recursos sem implementação validada permanecem indisponíveis; assinaturas pendentes não concedem acesso."}

class ModuleActivationIn(StrictModel):
    confirmed: bool

@app.post("/v1/company/modules/{module_id}/activate")
def activate_module(module_id: str,data: ModuleActivationIn,conn: DB,user: USER):
    require_role(user,"owner","admin")
    if not data.confirmed: raise HTTPException(422,"Confirme a proposta de ativação.")
    feature=conn.execute("SELECT * FROM feature_catalog WHERE id=?",(module_id,)).fetchone()
    if not feature: raise HTTPException(404,"Módulo não encontrado.")
    if not feature["available"] or MODULE_STATUSES.get(module_id)=="NOT_IMPLEMENTED": raise HTTPException(409,"Módulo ainda não está implementado.")
    if module_id not in IMPLEMENTED_FEATURES: raise HTTPException(409,"Este módulo tem funcionalidades operacionais parciais e não é ativável para operação completa.")
    profile=conn.execute("SELECT primary_segment,secondary_segments_json FROM company_profiles WHERE company_id=?",(user["company_id"],)).fetchone()
    if not profile: raise HTTPException(409,"Configure o perfil do negócio primeiro.")
    segments=[profile["primary_segment"],*json.loads(profile["secondary_segments_json"])]
    recommended=json.loads(feature["recommended_segments_json"])
    if "*" not in recommended and not any(x in recommended for x in segments): raise HTTPException(409,"Módulo incompatível com as atividades selecionadas.")
    for dep in json.loads(feature["dependencies_json"]):
        dep_row=conn.execute("SELECT available FROM feature_catalog WHERE id=?",(dep,)).fetchone()
        if not dep_row or not dep_row["available"]: raise HTTPException(409,"Dependência técnica indisponível.")
    sub=conn.execute("SELECT s.id,s.plan_id,s.status,s.starts_at,s.ends_at,p.status AS plan_status FROM company_subscriptions s JOIN plans p ON p.id=s.plan_id WHERE s.company_id=? ORDER BY s.created_at DESC LIMIT 1",(user["company_id"],)).fetchone()
    entitlement=conn.execute("SELECT 1 FROM company_module_entitlements WHERE company_id=? AND module_id=? AND status='granted'",(user["company_id"],module_id)).fetchone()
    now=now_iso()
    plan_current=bool(sub and sub["status"]=="active" and sub["plan_status"]=="active" and (not sub["starts_at"] or sub["starts_at"]<=now) and (not sub["ends_at"] or sub["ends_at"]>now))
    plan_ok=bool(plan_current and conn.execute("SELECT 1 FROM plan_entitlements WHERE plan_id=? AND module_id=? AND included=1",(sub["plan_id"],module_id)).fetchone())
    if not entitlement and not plan_ok: raise HTTPException(402,"Plano ou adicional sem entitlement para este módulo.")
    now=now_iso()
    with transaction(conn):
        prior=conn.execute("SELECT status FROM company_module_activations WHERE company_id=? AND module_id=?",(user["company_id"],module_id)).fetchone()
        if prior and prior["status"]=="ENABLED": return {"module_id":module_id,"status":"ENABLED","idempotent":True}
        conn.execute("INSERT INTO company_module_activations(company_id,module_id,status,activated_by,updated_at) VALUES(?,?,'ENABLED',?,?) ON CONFLICT(company_id,module_id) DO UPDATE SET status='ENABLED',activated_by=excluded.activated_by,updated_at=excluded.updated_at",(user["company_id"],module_id,user["id"],now))
        conn.execute("INSERT INTO module_events VALUES(?,?,?,?,?,?,?)",(new_id(),user["company_id"],module_id,"ENABLED",user["id"],json.dumps({"confirmed":True,"segments":segments},sort_keys=True),now))
        audit(conn,user["company_id"],user["id"],"module.activated","company_module",module_id,{"status":"ENABLED","subscription_id":sub["id"] if sub else None})
    return {"module_id":module_id,"status":"ENABLED","updated_at":now,"idempotent":False}


@app.get("/v1/company/profile/history")
def company_profile_history(conn: DB, user: USER):
    require_role(user,"owner","admin","manager")
    rows=conn.execute("SELECT * FROM company_profile_versions WHERE company_id=? ORDER BY created_at DESC LIMIT 100",
                      (user["company_id"],)).fetchall()
    return {"items":[{"id":r["id"],"profile":json.loads(r["profile_json"]),"actor_id":r["actor_id"],"created_at":r["created_at"]} for r in rows]}


@app.put("/v1/company/profile")
def update_company_profile(data: CompanyProfileIn, conn: DB, user: USER):
    require_role(user, "owner", "admin")
    if data.primary_segment not in SEGMENT_CATALOG or any(x not in SEGMENT_CATALOG for x in data.secondary_segments):
        raise HTTPException(422, "Selecione segmentos disponíveis no catálogo.")
    segments = list(dict.fromkeys([data.primary_segment, *data.secondary_segments]))
    question_ids = {q["id"] for seg in segments for q in SEGMENT_CATALOG[seg]["questions"]}
    if set(data.answers) - question_ids:
        raise HTTPException(422, "As respostas incluem campos que não pertencem aos segmentos selecionados.")
    modules = resolve_profile_modules(data.answers)
    updated = now_iso()
    with transaction(conn):
        snapshot = {"primary_segment":data.primary_segment,
                    "secondary_segments":list(dict.fromkeys(data.secondary_segments)),
                    "answers":data.answers,"modules":modules}
        conn.execute("INSERT INTO company_profile_versions VALUES(?,?,?,?,?)",
                     (new_id(),user["company_id"],json.dumps(snapshot,ensure_ascii=False,sort_keys=True),user["id"],updated))
        conn.execute("""INSERT INTO company_profiles(company_id,primary_segment,secondary_segments_json,answers_json,modules_json,updated_by,updated_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET primary_segment=excluded.primary_segment,
            secondary_segments_json=excluded.secondary_segments_json,answers_json=excluded.answers_json,
            modules_json=excluded.modules_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (user["company_id"],data.primary_segment,json.dumps(list(dict.fromkeys(data.secondary_segments))),
             json.dumps(data.answers,sort_keys=True),json.dumps(modules),user["id"],updated))
        audit(conn,user["company_id"],user["id"],"company.profile_configured","company_profile",user["company_id"],
              {"primary_segment":data.primary_segment,"secondary_segments":list(dict.fromkeys(data.secondary_segments)),
               "answers":data.answers,"modules":modules})
    return {"primary_segment":data.primary_segment,"secondary_segments":list(dict.fromkeys(data.secondary_segments)),
            "answers":data.answers,"modules":modules,"updated_at":updated,
            "status":"suggested","notice":"Esta configuração é auditável, mas não habilita funções ainda não implementadas nem comprova contratação de plano."}


@app.get("/v1/me")
def me(user: USER):
    return {"id": user["id"], "company_id": user["company_id"], "email": user["email"],
            "name": user["full_name"], "role": user["role"]}


@app.get("/v1/company/branches")
def list_branches(conn: DB,user: USER):
    rows=conn.execute("SELECT b.id,b.code,b.name,b.status FROM branches b JOIN branch_users bu ON bu.branch_id=b.id AND bu.company_id=b.company_id WHERE b.company_id=? AND bu.user_id=? AND b.status='active' ORDER BY b.created_at",(user["company_id"],user["id"])).fetchall()
    count=conn.execute("SELECT COUNT(*) FROM branches WHERE company_id=? AND status='active'",(user["company_id"],)).fetchone()[0]
    return {"items":[dict(r) for r in rows],"multi_branch":count>1,"restaurant_multi_branch_ready":False,"branch_count":count,"operational_data_partitioning":"partial"}

@app.post("/v1/company/branches",status_code=201)
def create_branch(data: BranchCreateIn,conn: DB,user: USER):
    require_role(user,"owner","admin")
    branch_id,created=new_id(),now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO branches(id,company_id,code,name,status,created_by,created_at) VALUES(?,?,?,?,'active',?,?)",(branch_id,user["company_id"],data.code.lower(),data.name.strip(),user["id"],created))
            conn.execute("INSERT INTO branch_users(company_id,branch_id,user_id) VALUES(?,?,?)",(user["company_id"],branch_id,user["id"]))
            audit(conn,user["company_id"],user["id"],"branch.created","branch",branch_id,{"code":data.code.lower(),"name":data.name.strip()})
    except sqlite3.IntegrityError: raise HTTPException(409,"Já existe uma filial com esse código.")
    return {"id":branch_id,"code":data.code.lower(),"name":data.name.strip(),"status":"active","operational_scope":"metadata_only"}

@app.get("/v1/company/branches/{branch_id}/users")
def list_branch_users(branch_id: str,conn: DB,user: USER):
    require_role(user,"owner","admin")
    require_permission(conn,user,"branches.manage")
    branch=conn.execute("SELECT id,code,name,status FROM branches WHERE id=? AND company_id=?",(branch_id,user["company_id"])).fetchone()
    if not branch: raise HTTPException(404,"Filial não encontrada nesta empresa.")
    rows=conn.execute("SELECT u.id,u.full_name,u.email,u.role,u.active FROM branch_users bu JOIN users u ON u.id=bu.user_id AND u.company_id=bu.company_id WHERE bu.company_id=? AND bu.branch_id=? ORDER BY u.full_name",(user["company_id"],branch_id)).fetchall()
    return {"branch":dict(branch),"users":[dict(r) for r in rows]}

@app.put("/v1/company/branches/{branch_id}/users")
def replace_branch_users(branch_id: str,data: BranchUsersIn,conn: DB,user: USER):
    require_role(user,"owner","admin")
    require_permission(conn,user,"branches.manage")
    requested=list(dict.fromkeys(data.user_ids))
    with transaction(conn):
        branch=conn.execute("SELECT id FROM branches WHERE id=? AND company_id=? AND status='active'",(branch_id,user["company_id"])).fetchone()
        if not branch: raise HTTPException(404,"Filial não encontrada ou inativa nesta empresa.")
        if requested:
            marks=",".join("?" for _ in requested)
            found=conn.execute(f"SELECT id FROM users WHERE company_id=? AND active=1 AND id IN ({marks})",(user["company_id"],*requested)).fetchall()
            if {r["id"] for r in found}!=set(requested): raise HTTPException(422,"Todos os usuários precisam estar ativos e pertencer à mesma empresa.")
        before=[r[0] for r in conn.execute("SELECT user_id FROM branch_users WHERE company_id=? AND branch_id=? ORDER BY user_id",(user["company_id"],branch_id)).fetchall()]
        conn.execute("DELETE FROM branch_users WHERE company_id=? AND branch_id=?",(user["company_id"],branch_id))
        for uid in requested: conn.execute("INSERT INTO branch_users(company_id,branch_id,user_id) VALUES(?,?,?)",(user["company_id"],branch_id,uid))
        audit(conn,user["company_id"],user["id"],"branch.users_replaced","branch",branch_id,{"before_user_ids":before,"after_user_ids":requested})
    return {"branch_id":branch_id,"user_ids":requested,"updated_at":now_iso()}

@app.post("/v1/branches/{branch_id}/registers/open",status_code=201)
def open_branch_register(branch_id: str,data: BranchRegisterOpenIn,conn: DB,user: USER):
    require_permission(conn,user,"cash.manage");require_branch_membership(conn,user,branch_id)
    register_id,now=new_id(),now_iso()
    try:
        with transaction(conn):
            if conn.execute("SELECT 1 FROM branch_cash_registers WHERE company_id=? AND branch_id=? AND status='open'",(user["company_id"],branch_id)).fetchone(): raise HTTPException(409,"Já existe caixa aberto nesta filial.")
            conn.execute("INSERT INTO branch_cash_registers(id,company_id,branch_id,label,status,opening_cents,opened_by,opened_at) VALUES(?,?,?,?,'open',?,?,?)",(register_id,user["company_id"],branch_id,data.label.strip(),decimal_money_cents(data.opening_amount),user["id"],now))
            audit(conn,user["company_id"],user["id"],"branch_register.opened","branch_cash_register",register_id,{"branch_id":branch_id,"label":data.label.strip()})
    except sqlite3.IntegrityError: raise HTTPException(409,"Já existe caixa aberto nesta filial.")
    return {"id":register_id,"branch_id":branch_id,"label":data.label.strip(),"status":"open","opening_amount":cents_to_money(decimal_money_cents(data.opening_amount)),"opened_at":now}

@app.get("/v1/branches/{branch_id}/registers/current")
def get_branch_register(branch_id: str,conn: DB,user: USER):
    require_permission(conn,user,"cash.manage");require_branch_membership(conn,user,branch_id)
    r=conn.execute("SELECT * FROM branch_cash_registers WHERE company_id=? AND branch_id=? AND status='open' ORDER BY opened_at DESC LIMIT 1",(user["company_id"],branch_id)).fetchone()
    if not r:return {"branch_id":branch_id,"register":None}
    rows=conn.execute("SELECT amount_cents FROM branch_cash_events WHERE company_id=? AND branch_id=? AND register_id=?",(user["company_id"],branch_id,r["id"])).fetchall()
    sales_cash=conn.execute("SELECT amount_cents FROM branch_cash_movements WHERE company_id=? AND branch_id=? AND register_id=?",(user["company_id"],branch_id,r["id"])).fetchall()
    expected=r["opening_cents"]+sum(x["amount_cents"] for x in rows)+sum(x["amount_cents"] for x in sales_cash)
    return {"branch_id":branch_id,"register":{"id":r["id"],"label":r["label"],"status":r["status"],"opening_amount":cents_to_money(r["opening_cents"]),"opened_at":r["opened_at"]},"expected_amount":cents_to_money(expected)}

@app.post("/v1/branches/{branch_id}/registers/{register_id}/movements",status_code=201)
def branch_register_movement(branch_id: str,register_id: str,data: BranchCashMovementIn,conn: DB,user: USER):
    require_permission(conn,user,"cash.manage");require_branch_membership(conn,user,branch_id)
    now=now_iso();delta=decimal_money_cents(data.amount)*(1 if data.kind=="supply" else -1)
    with transaction(conn):
        r=conn.execute("SELECT * FROM branch_cash_registers WHERE id=? AND company_id=? AND branch_id=? AND status='open'",(register_id,user["company_id"],branch_id)).fetchone()
        if not r:raise HTTPException(404,"Caixa aberto não encontrado nesta filial.")
        prior=conn.execute("SELECT amount_cents FROM branch_cash_events WHERE company_id=? AND branch_id=? AND register_id=?",(user["company_id"],branch_id,register_id)).fetchall()
        if r["opening_cents"]+sum(x[0] for x in prior)+delta<0:raise HTTPException(409,"Saldo insuficiente no caixa desta filial.")
        mid=new_id();conn.execute("INSERT INTO branch_cash_events(id,company_id,branch_id,register_id,actor_id,kind,amount_cents,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(mid,user["company_id"],branch_id,register_id,user["id"],data.kind,delta,data.reason.strip(),now))
        audit(conn,user["company_id"],user["id"],"branch_register.movement","branch_cash_register",register_id,{"branch_id":branch_id,"kind":data.kind,"amount":cents_to_money(abs(delta)),"reason":data.reason.strip()})
    return {"movement_id":mid,"branch_id":branch_id,"kind":data.kind,"amount":cents_to_money(abs(delta))}

@app.post("/v1/branches/{branch_id}/registers/{register_id}/close")
def close_branch_register(branch_id: str,register_id: str,data: BranchRegisterCloseIn,conn: DB,user: USER):
    require_permission(conn,user,"cash.manage");require_branch_membership(conn,user,branch_id)
    with transaction(conn):
        r=conn.execute("SELECT * FROM branch_cash_registers WHERE id=? AND company_id=? AND branch_id=? AND status='open'",(register_id,user["company_id"],branch_id)).fetchone()
        if not r:raise HTTPException(404,"Caixa aberto não encontrado nesta filial.")
        ev=conn.execute("SELECT amount_cents FROM branch_cash_events WHERE company_id=? AND branch_id=? AND register_id=?",(user["company_id"],branch_id,register_id)).fetchall()
        sales_and_returns=conn.execute("SELECT amount_cents FROM branch_cash_movements WHERE company_id=? AND branch_id=? AND register_id=?",(user["company_id"],branch_id,register_id)).fetchall()
        expected=r["opening_cents"]+sum(x[0] for x in ev)+sum(x[0] for x in sales_and_returns);counted=decimal_money_cents(data.counted_amount);now=now_iso()
        conn.execute("UPDATE branch_cash_registers SET status='closed',closed_by=?,closed_at=?,counted_cents=?,expected_cents=?,difference_cents=? WHERE id=? AND company_id=? AND branch_id=?",(user["id"],now,counted,expected,counted-expected,register_id,user["company_id"],branch_id))
        audit(conn,user["company_id"],user["id"],"branch_register.closed","branch_cash_register",register_id,{"branch_id":branch_id,"expected":cents_to_money(expected),"counted":cents_to_money(counted)})
    return {"id":register_id,"branch_id":branch_id,"status":"closed","expected_amount":cents_to_money(expected),"counted_amount":cents_to_money(counted),"difference":cents_to_money(counted-expected),"closed_at":now}

@app.post("/v1/branches/{branch_id}/sales",status_code=201)
def create_branch_sale(branch_id: str,data: BranchSaleIn,request: Request,conn: DB,user: USER,
                      idempotency_key: Annotated[str|None,Header(alias="Idempotency-Key")]=None,response: Response=None):
    require_permission(conn,user,"sales.create");require_branch_membership(conn,user,branch_id)
    if not idempotency_key or len(idempotency_key)>128:raise HTTPException(400,"Envie Idempotency-Key para evitar venda duplicada.")
    if any(x.discount>0 for x in data.items) and user["role"] not in {"owner","admin","manager"}:raise HTTPException(403,"Descontos exigem gerente.")
    qtys={};lines=[];total=0
    for item in data.items:
        q=decimal_milli(item.quantity);d=decimal_money_cents(item.discount)
        product=conn.execute("SELECT id,sku,name,price_cents,unit FROM products WHERE id=? AND company_id=? AND active=1",(item.product_id,user["company_id"])).fetchone()
        if not product:raise HTTPException(404,"Produto não encontrado nesta empresa.")
        gross=int((Decimal(product["price_cents"])*Decimal(q)/Decimal(1000)).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
        if d>gross:raise HTTPException(422,"Desconto não pode superar o valor do item.")
        qtys[item.product_id]=qtys.get(item.product_id,0)+q;line_total=gross-d;total+=line_total;lines.append((product,q,d,line_total))
    if data.customer_id and not conn.execute("SELECT 1 FROM customers WHERE id=? AND company_id=? AND active=1",(data.customer_id,user["company_id"])).fetchone():raise HTTPException(404,"Cliente não encontrado nesta empresa.")
    received=decimal_money_cents(data.received_amount)
    if received<total:raise HTTPException(422,"Valor recebido não cobre o total.")
    normalized={"branch_id":branch_id,"items":[{"product_id":x.product_id,"quantity":str(x.quantity),"discount":str(x.discount)} for x in data.items],"received_amount":str(data.received_amount),"customer_id":data.customer_id}
    req_hash=hashlib.sha256(json.dumps(normalized,sort_keys=True,separators=(",",":")).encode()).hexdigest();sale_id,now=new_id(),now_iso()
    with transaction(conn):
        prior=conn.execute("SELECT * FROM branch_sales WHERE company_id=? AND branch_id=? AND idempotency_key=?",(user["company_id"],branch_id,idempotency_key)).fetchone()
        if prior:
            if prior["request_hash"]!=req_hash:raise HTTPException(409,"Chave de idempotência usada com dados diferentes.")
            if response is not None:response.status_code=200
            return {"id":prior["id"],"branch_id":branch_id,"status":prior["status"],"total":cents_to_money(prior["total_cents"]),"idempotent":True}
        register=conn.execute("SELECT * FROM branch_cash_registers WHERE company_id=? AND branch_id=? AND status='open'",(user["company_id"],branch_id)).fetchone()
        if not register:raise HTTPException(409,"Abra o caixa desta filial antes da venda.")
        available={}
        for product_id,qty in qtys.items():
            row=conn.execute("SELECT quantity_milli FROM branch_product_stock WHERE company_id=? AND branch_id=? AND product_id=?",(user["company_id"],branch_id,product_id)).fetchone()
            balance=int(row[0]) if row else 0
            if balance<qty:raise HTTPException(409,"Estoque insuficiente nesta filial.")
            available[product_id]=balance
        conn.execute("INSERT INTO branch_sales(id,company_id,branch_id,register_id,created_by,idempotency_key,request_hash,status,subtotal_cents,total_cents,payment_method,received_cents,change_cents,created_at,customer_id) VALUES(?,?,?,?,?,?,?,'paid',?,?,'cash',?,?,?,?)",(sale_id,user["company_id"],branch_id,register["id"],user["id"],idempotency_key,req_hash,total,total,received,received-total,now,data.customer_id))
        for prod,q,d,line_total in lines:conn.execute("INSERT INTO branch_sale_items(id,company_id,branch_id,sale_id,product_id,sku,name,quantity_milli,unit_price_cents,discount_cents,line_total_cents) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(new_id(),user["company_id"],branch_id,sale_id,prod["id"],prod["sku"],prod["name"],q,prod["price_cents"],d,line_total))
        for pid,q in qtys.items():
            before=available[pid];after=before-q
            conn.execute("UPDATE branch_product_stock SET quantity_milli=?,updated_at=? WHERE company_id=? AND branch_id=? AND product_id=?",(after,now,user["company_id"],branch_id,pid))
            if not conn.execute("SELECT 1 FROM branch_product_stock WHERE company_id=? AND branch_id=? AND product_id=?",(user["company_id"],branch_id,pid)).fetchone():conn.execute("INSERT INTO branch_product_stock(company_id,branch_id,product_id,quantity_milli,updated_at) VALUES(?,?,?,?,?)",(user["company_id"],branch_id,pid,after,now))
            conn.execute("INSERT INTO branch_stock_events(id,company_id,branch_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,transfer_id,created_at) VALUES(?,?,?,?,?,'sale',?,?,?,?,NULL,?)",(new_id(),user["company_id"],branch_id,pid,user["id"],-q,before,after,"Venda "+sale_id,now))
        conn.execute("INSERT INTO branch_cash_movements(id,company_id,branch_id,register_id,actor_id,kind,amount_cents,reason,sale_id,created_at) VALUES(?,?,?,?,?,'sale',?,?,?,?)",(new_id(),user["company_id"],branch_id,register["id"],user["id"],total,"Venda "+sale_id,sale_id,now))
        audit(conn,user["company_id"],user["id"],"branch_sale.completed","branch_sale",sale_id,{"branch_id":branch_id,"total":cents_to_money(total),"item_count":len(lines),"cash_only":True})
    return {"id":sale_id,"branch_id":branch_id,"status":"paid","total":cents_to_money(total),"payment_method":"cash","change":cents_to_money(received-total),"idempotent":False}

def require_branch_membership(conn,user,branch_id):
    branch=conn.execute("SELECT id FROM branches WHERE id=? AND company_id=? AND status='active'",(branch_id,user["company_id"])).fetchone()
    if not branch: raise HTTPException(404,"Filial não encontrada ou inativa nesta empresa.")
    member=conn.execute("SELECT 1 FROM branch_users WHERE company_id=? AND branch_id=? AND user_id=?",(user["company_id"],branch_id,user["id"])).fetchone()
    if not member: raise HTTPException(403,"Usuário sem acesso a esta filial.")
    return branch

@app.get("/v1/branches/{branch_id}/dashboard/summary")
def branch_dashboard_summary(branch_id: str,conn: DB,user: USER):
    require_permission(conn,user,"reports.read");require_branch_membership(conn,user,branch_id)
    today=date.today().isoformat();row=conn.execute("SELECT COUNT(*) AS n,COALESCE(SUM(total_cents),0) AS cents FROM branch_sales WHERE company_id=? AND branch_id=? AND status='paid' AND substr(created_at,1,10)=?",(user["company_id"],branch_id,today)).fetchone()
    count=conn.execute("SELECT COUNT(*) FROM products WHERE company_id=? AND active=1",(user["company_id"],)).fetchone()[0]
    low=conn.execute("SELECT COUNT(*) FROM products p LEFT JOIN branch_product_stock b ON b.product_id=p.id AND b.company_id=p.company_id AND b.branch_id=? WHERE p.company_id=? AND p.active=1 AND COALESCE(b.quantity_milli,0)<=p.min_stock_milli",(branch_id,user["company_id"])).fetchone()[0]
    register=conn.execute("SELECT id,label,opening_cents,opened_at FROM branch_cash_registers WHERE company_id=? AND branch_id=? AND status='open' ORDER BY opened_at DESC LIMIT 1",(user["company_id"],branch_id)).fetchone()
    expected=None
    if register:
        ev=conn.execute("SELECT amount_cents FROM branch_cash_events WHERE company_id=? AND branch_id=? AND register_id=?",(user["company_id"],branch_id,register["id"])).fetchall();sales=conn.execute("SELECT amount_cents FROM branch_cash_movements WHERE company_id=? AND branch_id=? AND register_id=?",(user["company_id"],branch_id,register["id"])).fetchall();expected=register["opening_cents"]+sum(x[0] for x in ev)+sum(x[0] for x in sales)
    return {"branch_id":branch_id,"sales_today":row["n"],"revenue_today":cents_to_money(row["cents"]),"active_products":count,"low_stock_products":low,"register":{"id":register["id"],"label":register["label"],"opening_amount":cents_to_money(register["opening_cents"]),"expected_amount":cents_to_money(expected)} if register else None}

@app.get("/v1/branches/{branch_id}/reports/inventory")
def branch_inventory_report(branch_id: str,conn: DB,user: USER,limit: int=Query(default=500,ge=1,le=500),search: str=Query(default="",max_length=100)):
    require_permission(conn,user,"reports.read");require_branch_membership(conn,user,branch_id)
    term="%"+search.strip().replace("%","\%").replace("_","\_")+"%"
    rows=conn.execute("SELECT p.sku,p.name,p.category,p.unit,p.cost_cents,p.price_cents,p.min_stock_milli,COALESCE(b.quantity_milli,0) AS q FROM products p LEFT JOIN branch_product_stock b ON b.company_id=p.company_id AND b.product_id=p.id AND b.branch_id=? WHERE p.company_id=? AND p.active=1 AND (p.name LIKE ? ESCAPE '\\' OR p.sku LIKE ? ESCAPE '\\') ORDER BY p.name LIMIT ?",(branch_id,user["company_id"],term,term,limit)).fetchall()
    items=[]
    for r in rows:
        q=int(r["q"]);cost=(q*r["cost_cents"]+500)//1000;retail=(q*r["price_cents"]+500)//1000
        items.append({"sku":r["sku"],"name":r["name"],"category":r["category"],"unit":r["unit"],"stock":milli_to_decimal(q),"minimum_stock":milli_to_decimal(r["min_stock_milli"]),"unit_cost":cents_to_money(r["cost_cents"]),"cost_value":cents_to_money(cost),"unit_price":cents_to_money(r["price_cents"]),"retail_value":cents_to_money(retail),"low_stock":q<=r["min_stock_milli"]})
    return {"branch_id":branch_id,"items":items,"total":len(items),"summary":{"product_count":len(items),"estimated_cost_value":cents_to_money(sum((int(r['q'])*int(r['cost_cents'])+500)//1000 for r in rows)),"estimated_retail_value":cents_to_money(sum((int(r['q'])*int(r['price_cents'])+500)//1000 for r in rows)),"low_stock_products":sum(1 for r in rows if int(r['q'])<=r['min_stock_milli'])},"note":"Relatório calculado somente sobre o ledger desta filial; estoque legado sem atribuição foi excluído."}

@app.get("/v1/branches/{branch_id}/sales")
def list_branch_sales(branch_id: str,conn: DB,user: USER,limit: int=Query(default=100,ge=1,le=200)):
    require_permission(conn,user,"sales.read");require_branch_membership(conn,user,branch_id)
    rows=conn.execute("SELECT * FROM branch_sales WHERE company_id=? AND branch_id=? ORDER BY created_at DESC LIMIT ?",(user["company_id"],branch_id,limit)).fetchall();items=[]
    for r in rows:
        lines=conn.execute("SELECT product_id,name,sku,quantity_milli,unit_price_cents,line_total_cents FROM branch_sale_items WHERE company_id=? AND branch_id=? AND sale_id=?",(user["company_id"],branch_id,r["id"])).fetchall()
        items.append({"id":r["id"],"status":r["status"],"created_at":r["created_at"],"total":cents_to_money(r["total_cents"]),"change":cents_to_money(r["change_cents"]),"payment_method":"cash","items":[{"product_id":i["product_id"],"name":i["name"],"sku":i["sku"],"quantity":milli_to_decimal(i["quantity_milli"]),"line_total":cents_to_money(i["line_total_cents"])} for i in lines]})
    return {"branch_id":branch_id,"items":items,"total":len(items)}

@app.get("/v1/branches/{branch_id}/sales/report")
def branch_sales_report(branch_id: str,start_date: date,end_date: date,group_by: str="day",conn: DB=None,user: USER=None):
    require_permission(conn,user,"reports.read");require_branch_membership(conn,user,branch_id)
    if end_date<start_date or (end_date-start_date).days>366:raise HTTPException(422,"Período inválido.")
    if group_by not in {"day","week","month"}:raise HTTPException(422,"Agrupamento inválido.")
    rows=conn.execute("SELECT status,total_cents,created_at FROM branch_sales WHERE company_id=? AND branch_id=? AND substr(created_at,1,10) BETWEEN ? AND ?",(user["company_id"],branch_id,start_date.isoformat(),end_date.isoformat())).fetchall();groups={}
    for row in rows:
        d=row["created_at"][:10]
        key=d if group_by=="day" else d[:7] if group_by=="month" else date.fromisoformat(d).strftime("%G-W%V")
        g=groups.setdefault(key,{"period":key,"sale_count":0,"paid_count":0,"voided_count":0,"gross_paid_cents":0,"voided_value_cents":0})
        g["sale_count"]+=1
        if row["status"]=="paid":g["paid_count"]+=1;g["gross_paid_cents"]+=row["total_cents"]
        else:g["voided_count"]+=1;g["voided_value_cents"]+=row["total_cents"]
    items=[{**g,"gross_paid":cents_to_money(g.pop("gross_paid_cents")),"voided_value":cents_to_money(g.pop("voided_value_cents"))} for _,g in sorted(groups.items())]
    return {"branch_id":branch_id,"start_date":start_date.isoformat(),"end_date":end_date.isoformat(),"items":items,"totals":{"sale_count":sum(x["sale_count"] for x in items),"paid_count":sum(x["paid_count"] for x in items),"voided_count":sum(x["voided_count"] for x in items),"gross_paid":cents_to_money(sum(r["total_cents"] for r in rows if r["status"]=="paid")),"voided_value":cents_to_money(sum(r["total_cents"] for r in rows if r["status"]!="paid"))}}

@app.get("/v1/branches/{branch_id}/products")
def list_branch_products(branch_id: str,conn: DB,user: USER,search: str=Query(default="",max_length=100),limit: int=Query(default=200,ge=1,le=500)):
    require_permission(conn,user,"products.read");require_branch_membership(conn,user,branch_id)
    term="%"+search.strip().replace("%","\%").replace("_","\_")+"%"
    rows=conn.execute("SELECT p.id,p.sku,p.name,p.unit,p.price_cents,p.cost_cents,p.category,p.barcode,p.min_stock_milli,COALESCE(bs.quantity_milli,0) AS branch_stock FROM products p LEFT JOIN branch_product_stock bs ON bs.company_id=p.company_id AND bs.product_id=p.id AND bs.branch_id=? WHERE p.company_id=? AND p.active=1 AND (p.name LIKE ? ESCAPE '\\' OR p.sku LIKE ? ESCAPE '\\' OR COALESCE(p.barcode,'') LIKE ? ESCAPE '\\') ORDER BY p.name LIMIT ?",(branch_id,user["company_id"],term,term,term,limit)).fetchall()
    return {"branch_id":branch_id,"total":len(rows),"items":[{"id":r["id"],"sku":r["sku"],"name":r["name"],"unit":r["unit"],"price":cents_to_money(r["price_cents"]),"cost":cents_to_money(r["cost_cents"]),"category":r["category"],"barcode":r["barcode"],"minimum_stock":milli_to_decimal(r["min_stock_milli"]),"stock":milli_to_decimal(r["branch_stock"]),"active":True,"low_stock":int(r["branch_stock"])<=int(r["min_stock_milli"])} for r in rows]}

@app.get("/v1/branches/{branch_id}/inventory")
def list_branch_inventory(branch_id: str,conn: DB,user: USER,search: str=Query(default="",max_length=100)):
    require_permission(conn,user,"inventory.read");require_branch_membership(conn,user,branch_id)
    if not conn.execute("SELECT 1 FROM branches WHERE company_id=? AND id=?",(user["company_id"],branch_id)).fetchone(): raise HTTPException(404,"Filial não encontrada.")
    term="%"+search.strip().replace("%","\%").replace("_","\_")+"%"
    rows=conn.execute("""SELECT p.id,p.sku,p.name,p.unit,p.price_cents,p.cost_cents,p.category,p.barcode,
        COALESCE(bps.quantity_milli,0) AS quantity_milli,bps.updated_at
        FROM products p LEFT JOIN branch_product_stock bps ON bps.product_id=p.id AND bps.company_id=p.company_id AND bps.branch_id=?
        WHERE p.company_id=? AND p.active=1 AND (?='' OR p.name LIKE ? ESCAPE '\' OR p.sku LIKE ? ESCAPE '\' OR COALESCE(p.barcode,'') LIKE ? ESCAPE '\')
        ORDER BY p.name""",(branch_id,user["company_id"],search.strip(),term,term,term)).fetchall()
    return {"branch_id":branch_id,"items":[{"product_id":r["id"],"sku":r["sku"],"name":r["name"],"unit":r["unit"],"category":r["category"],"barcode":r["barcode"],"quantity":milli_to_decimal(r["quantity_milli"]),"updated_at":r["updated_at"]} for r in rows],"legacy_unassigned_excluded":True}

@app.post("/v1/branches/{branch_id}/inventory/{product_id}/adjustments",status_code=201)
def adjust_branch_stock(branch_id: str,product_id: str,data: StockAdjustmentIn,conn: DB,user: USER):
    require_permission(conn,user,"stock.adjust");require_branch_membership(conn,user,branch_id)
    movement_id,now=new_id(),now_iso();qty=decimal_milli(data.quantity)
    with transaction(conn):
        product=conn.execute("SELECT id FROM products WHERE id=? AND company_id=? AND active=1",(product_id,user["company_id"])).fetchone()
        if not product: raise HTTPException(404,"Produto não encontrado nesta empresa.")
        row=conn.execute("SELECT quantity_milli FROM branch_product_stock WHERE company_id=? AND branch_id=? AND product_id=?",(user["company_id"],branch_id,product_id)).fetchone()
        before=int(row[0]) if row else 0;after=before+qty if data.direction=="in" else before-qty
        if after<0: raise HTTPException(409,"Saldo insuficiente nesta filial.")
        conn.execute("INSERT INTO branch_product_stock(company_id,branch_id,product_id,quantity_milli,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(company_id,branch_id,product_id) DO UPDATE SET quantity_milli=excluded.quantity_milli,updated_at=excluded.updated_at",(user["company_id"],branch_id,product_id,after,now))
        conn.execute("INSERT INTO branch_stock_events(id,company_id,branch_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,transfer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)",(movement_id,user["company_id"],branch_id,product_id,user["id"],"adjustment_in" if data.direction=="in" else "adjustment_out",qty if data.direction=="in" else -qty,before,after,data.reason,now))
        audit(conn,user["company_id"],user["id"],"branch_stock.adjusted","branch_product_stock",branch_id+":"+product_id,{"movement_id":movement_id,"direction":data.direction,"before":milli_to_decimal(before),"after":milli_to_decimal(after),"reason":data.reason})
    return {"movement_id":movement_id,"branch_id":branch_id,"product_id":product_id,"before":milli_to_decimal(before),"after":milli_to_decimal(after)}

@app.get("/v1/branches/{branch_id}/stock-movements")
def list_branch_stock_events(branch_id: str,conn: DB,user: USER,product_id: str|None=None,kind: str|None=None,limit: int=Query(default=100,ge=1,le=200)):
    require_permission(conn,user,"inventory.read");require_branch_membership(conn,user,branch_id)
    clauses=["e.company_id=?","e.branch_id=?"];params=[user["company_id"],branch_id]
    if product_id: clauses.append("e.product_id=?");params.append(product_id)
    if kind:
        if kind not in {"adjustment_in","adjustment_out","transfer_in","transfer_out","sale","reversal"}:raise HTTPException(422,"Tipo de movimento inválido.")
        clauses.append("e.kind=?");params.append(kind)
    rows=conn.execute(f"SELECT e.*,p.name AS product,p.sku,u.full_name AS actor FROM branch_stock_events e JOIN products p ON p.id=e.product_id AND p.company_id=e.company_id JOIN users u ON u.id=e.actor_id AND u.company_id=e.company_id WHERE {' AND '.join(clauses)} ORDER BY e.created_at DESC,e.id DESC LIMIT ?",(*params,limit)).fetchall()
    return {"branch_id":branch_id,"items":[{"id":r["id"],"product_id":r["product_id"],"product":r["product"],"sku":r["sku"],"actor":r["actor"],"kind":r["kind"],"quantity":milli_to_decimal(abs(r["quantity_milli"])),"before":milli_to_decimal(r["before_milli"]),"after":milli_to_decimal(r["after_milli"]),"reason":r["reason"],"transfer_id":r["transfer_id"],"created_at":r["created_at"]} for r in rows],"total":len(rows)}

@app.post("/v1/branches/stock-transfers",status_code=201)
def transfer_branch_stock(data: BranchStockTransferIn,conn: DB,user: USER,idempotency_key: Annotated[str|None,Header(alias="Idempotency-Key")]=None,response: Response=None):
    require_permission(conn,user,"stock.adjust")
    if not idempotency_key or len(idempotency_key)>128: raise HTTPException(400,"Envie Idempotency-Key para evitar transferências duplicadas.")
    if data.from_branch_id==data.to_branch_id: raise HTTPException(422,"Escolha duas filiais diferentes.")
    require_branch_membership(conn,user,data.from_branch_id);require_branch_membership(conn,user,data.to_branch_id)
    qty=decimal_milli(data.quantity);transfer_id,now=new_id(),now_iso()
    body={"from_branch_id":data.from_branch_id,"to_branch_id":data.to_branch_id,"product_id":data.product_id,"quantity_milli":qty,"reason":data.reason}
    request_hash=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    with transaction(conn):
        prior=conn.execute("SELECT * FROM branch_stock_transfers WHERE company_id=? AND idempotency_key=?",(user["company_id"],idempotency_key)).fetchone()
        if prior:
            if prior["request_hash"]!=request_hash: raise HTTPException(409,"Idempotency-Key já foi usada com dados diferentes.")
            if response is not None: response.status_code=200
            return {"transfer_id":prior["id"],"product_id":prior["product_id"],"quantity":milli_to_decimal(prior["quantity_milli"]),"from_branch_id":prior["from_branch_id"],"to_branch_id":prior["to_branch_id"],"status":"completed","idempotent":True}
        if not conn.execute("SELECT 1 FROM products WHERE id=? AND company_id=? AND active=1",(data.product_id,user["company_id"])).fetchone(): raise HTTPException(404,"Produto não encontrado nesta empresa.")
        src=conn.execute("SELECT quantity_milli FROM branch_product_stock WHERE company_id=? AND branch_id=? AND product_id=?",(user["company_id"],data.from_branch_id,data.product_id)).fetchone()
        source_before=int(src[0]) if src else 0
        if source_before<qty: raise HTTPException(409,"Saldo insuficiente na filial de origem.")
        dst=conn.execute("SELECT quantity_milli FROM branch_product_stock WHERE company_id=? AND branch_id=? AND product_id=?",(user["company_id"],data.to_branch_id,data.product_id)).fetchone()
        dest_before=int(dst[0]) if dst else 0
        source_after=source_before-qty;dest_after=dest_before+qty
        for bid,bal in ((data.from_branch_id,source_after),(data.to_branch_id,dest_after)):
            conn.execute("INSERT INTO branch_product_stock(company_id,branch_id,product_id,quantity_milli,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(company_id,branch_id,product_id) DO UPDATE SET quantity_milli=excluded.quantity_milli,updated_at=excluded.updated_at",(user["company_id"],bid,data.product_id,bal,now))
        conn.execute("INSERT INTO branch_stock_events(id,company_id,branch_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,transfer_id,created_at) VALUES(?,?,?,?,?,'transfer_out',?,?,?,?,?,?)",(new_id(),user["company_id"],data.from_branch_id,data.product_id,user["id"],-qty,source_before,source_after,data.reason,transfer_id,now))
        conn.execute("INSERT INTO branch_stock_events(id,company_id,branch_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,transfer_id,created_at) VALUES(?,?,?,?,?,'transfer_in',?,?,?,?,?,?)",(new_id(),user["company_id"],data.to_branch_id,data.product_id,user["id"],qty,dest_before,dest_after,data.reason,transfer_id,now))
        conn.execute("INSERT INTO branch_stock_transfers(id,company_id,product_id,from_branch_id,to_branch_id,quantity_milli,reason,actor_id,idempotency_key,request_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(transfer_id,user["company_id"],data.product_id,data.from_branch_id,data.to_branch_id,qty,data.reason,user["id"],idempotency_key,request_hash,now))
        audit(conn,user["company_id"],user["id"],"branch_stock.transferred","branch_stock_transfer",transfer_id,{"product_id":data.product_id,"from_branch_id":data.from_branch_id,"to_branch_id":data.to_branch_id,"quantity":milli_to_decimal(qty),"reason":data.reason})
    return {"transfer_id":transfer_id,"product_id":data.product_id,"quantity":milli_to_decimal(qty),"from_branch_id":data.from_branch_id,"to_branch_id":data.to_branch_id,"status":"completed","idempotent":False}

@app.post("/v1/branches/{branch_id}/sales/{sale_id}/void")
def void_branch_sale(branch_id: str,sale_id: str,data: SaleVoidIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager");require_permission(conn,user,"sales.void");require_branch_membership(conn,user,branch_id)
    now=now_iso();rid=new_id()
    with transaction(conn):
        sale=conn.execute("SELECT * FROM branch_sales WHERE id=? AND company_id=? AND branch_id=?",(sale_id,user["company_id"],branch_id)).fetchone()
        if not sale:raise HTTPException(404,"Venda não encontrada nesta filial.")
        if sale["status"]!="paid":raise HTTPException(409,"Venda já cancelada ou não paga.")
        events=conn.execute("SELECT amount_cents FROM branch_cash_events WHERE company_id=? AND branch_id=? AND register_id=?",(user["company_id"],branch_id,sale["register_id"])).fetchall()
        movements=conn.execute("SELECT amount_cents FROM branch_cash_movements WHERE company_id=? AND branch_id=? AND register_id=?",(user["company_id"],branch_id,sale["register_id"])).fetchall()
        register=conn.execute("SELECT * FROM branch_cash_registers WHERE id=? AND company_id=? AND branch_id=? AND status='open'",(sale["register_id"],user["company_id"],branch_id)).fetchone()
        if not register:raise HTTPException(409,"É necessário caixa aberto nesta filial para registrar devolução em dinheiro.")
        expected=register["opening_cents"]+sum(x[0] for x in events)+sum(x[0] for x in movements)
        if expected<sale["total_cents"]:raise HTTPException(409,"Saldo insuficiente no caixa desta filial.")
        lines=conn.execute("SELECT * FROM branch_sale_items WHERE sale_id=? AND company_id=? AND branch_id=?",(sale_id,user["company_id"],branch_id)).fetchall()
        for line in lines:
            row=conn.execute("SELECT quantity_milli FROM branch_product_stock WHERE company_id=? AND branch_id=? AND product_id=?",(user["company_id"],branch_id,line["product_id"])).fetchone()
            before=int(row[0]) if row else 0;after=before+line["quantity_milli"]
            conn.execute("INSERT INTO branch_product_stock(company_id,branch_id,product_id,quantity_milli,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(company_id,branch_id,product_id) DO UPDATE SET quantity_milli=excluded.quantity_milli,updated_at=excluded.updated_at",(user["company_id"],branch_id,line["product_id"],after,now))
            conn.execute("INSERT INTO branch_stock_events(id,company_id,branch_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,transfer_id,created_at) VALUES(?,?,?,?,?,'reversal',?,?,?,?,NULL,?)",(new_id(),user["company_id"],branch_id,line["product_id"],user["id"],line["quantity_milli"],before,after,"Cancelamento "+sale_id+": "+data.reason,now))
        conn.execute("UPDATE branch_sales SET status='voided' WHERE id=? AND company_id=? AND branch_id=?",(sale_id,user["company_id"],branch_id))
        conn.execute("INSERT INTO branch_cash_movements(id,company_id,branch_id,register_id,actor_id,kind,amount_cents,reason,sale_id,created_at) VALUES(?,?,?,?,?,'withdrawal',?,?,?,?)",(rid,user["company_id"],branch_id,register["id"],user["id"],-sale["total_cents"],"Estorno "+sale_id+": "+data.reason,sale_id,now))
        conn.execute("INSERT INTO branch_sales_returns(id,company_id,branch_id,sale_id,actor_id,amount_cents,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",(rid,user["company_id"],branch_id,sale_id,user["id"],sale["total_cents"],data.reason,now))
        audit(conn,user["company_id"],user["id"],"branch_sale.voided","branch_sale",sale_id,{"branch_id":branch_id,"return_id":rid,"reason":data.reason,"amount":cents_to_money(sale["total_cents"])})
    return {"id":sale_id,"branch_id":branch_id,"status":"voided","return_id":rid,"amount":cents_to_money(sale["total_cents"]),"refund_method":"cash_recorded_manual","note":"Devolução contabilizada no caixa da filial; entrega física do dinheiro é manual."}

@app.get("/v1/company/permissions")
def list_permissions(conn: DB,user: USER):
    require_role(user,"owner","admin")
    rows=conn.execute("SELECT role,permission,allowed FROM role_permissions WHERE company_id=?",(user["company_id"],)).fetchall()
    overrides={(r["role"],r["permission"]):bool(r["allowed"]) for r in rows}
    return {"catalog":[{"id":k,"name":v} for k,v in PERMISSION_CATALOG.items()],"roles":{role:{perm:overrides.get((role,perm),perm in ROLE_PERMISSIONS.get(role,set())) for perm in PERMISSION_CATALOG} for role in sorted(ROLES)}}

@app.get("/v1/permissions/effective")
def effective_permission(conn: DB,user: USER,permission: str=Query(min_length=3,max_length=80)):
    if permission not in PERMISSION_CATALOG: raise HTTPException(404,"Permissão desconhecida.")
    override=conn.execute("SELECT allowed FROM user_permissions WHERE company_id=? AND user_id=? AND permission=?",(user["company_id"],user["id"],permission)).fetchone()
    role=conn.execute("SELECT allowed FROM role_permissions WHERE company_id=? AND role=? AND permission=?",(user["company_id"],user["role"],permission)).fetchone()
    allowed=bool(override[0]) if override is not None else bool(role[0]) if role is not None else permission in ROLE_PERMISSIONS.get(user["role"],set())
    return {"permission":permission,"allowed":allowed,"source":"user" if override is not None else "role" if role is not None else "default"}

@app.put("/v1/company/permissions/role")
def set_role_permission(data: RolePermissionIn,conn: DB,user: USER):
    require_role(user,"owner")
    if data.permission not in PERMISSION_CATALOG: raise HTTPException(404,"Permissão desconhecida.")
    now=now_iso()
    with transaction(conn):
        conn.execute("INSERT INTO role_permissions(company_id,role,permission,allowed,updated_by,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(company_id,role,permission) DO UPDATE SET allowed=excluded.allowed,updated_by=excluded.updated_by,updated_at=excluded.updated_at",(user["company_id"],data.role,data.permission,int(data.allowed),user["id"],now))
        audit(conn,user["company_id"],user["id"],"permission.role_updated","role_permission",data.role,{"permission":data.permission,"allowed":data.allowed})
    return {"role":data.role,"permission":data.permission,"allowed":data.allowed,"updated_at":now}

@app.put("/v1/users/{user_id}/permissions")
def set_user_permission(user_id: str,data: UserPermissionIn,conn: DB,user: USER):
    require_role(user,"owner")
    if data.permission not in PERMISSION_CATALOG: raise HTTPException(404,"Permissão desconhecida.")
    target=conn.execute("SELECT id,role FROM users WHERE id=? AND company_id=? AND active=1",(user_id,user["company_id"])).fetchone()
    if not target: raise HTTPException(404,"Usuário não encontrado nesta empresa.")
    if target["role"]=="owner": raise HTTPException(409,"Permissões do titular não podem ser sobrescritas.")
    if data.allowed and data.permission not in ROLE_PERMISSIONS.get(target["role"],set()): raise HTTPException(409,"Permissão fora do perfil-base.")
    now=now_iso()
    with transaction(conn):
        conn.execute("INSERT INTO user_permissions(company_id,user_id,permission,allowed,updated_by,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(company_id,user_id,permission) DO UPDATE SET allowed=excluded.allowed,updated_by=excluded.updated_by,updated_at=excluded.updated_at",(user["company_id"],user_id,data.permission,int(data.allowed),user["id"],now))
        audit(conn,user["company_id"],user["id"],"permission.user_updated","user_permission",user_id,{"permission":data.permission,"allowed":data.allowed})
    return {"user_id":user_id,"permission":data.permission,"allowed":data.allowed,"updated_at":now}

@app.get("/v1/master/plans")
def master_list_plans(conn: DB,bootstrap_key: Annotated[str | None,Header(alias="X-Bootstrap-Key")]=None):
    if not BOOTSTRAP_SECRET or not bootstrap_key or not hmac.compare_digest(bootstrap_key,BOOTSTRAP_SECRET): raise HTTPException(403,"Acesso administrativo não autorizado.")
    rows=conn.execute("SELECT * FROM plans ORDER BY name").fetchall()
    return {"items":[{"id":r["id"],"name":r["name"],"status":r["status"],"price":cents_to_money(r["price_cents"]) if r["price_cents"] is not None else None,"billing_period":r["billing_period"],"included_modules":[x[0] for x in conn.execute("SELECT module_id FROM plan_entitlements WHERE plan_id=? AND included=1",(r["id"],)).fetchall()]} for r in rows]}

@app.post("/v1/master/plans",status_code=201)
def master_create_plan(data: PlanCreateIn,conn: DB,bootstrap_key: Annotated[str | None,Header(alias="X-Bootstrap-Key")]=None):
    if not BOOTSTRAP_SECRET or not bootstrap_key or not hmac.compare_digest(bootstrap_key,BOOTSTRAP_SECRET): raise HTTPException(403,"Acesso administrativo não autorizado.")
    if any(m not in IMPLEMENTED_FEATURES for m in data.included_modules): raise HTTPException(422,"Plano não pode incluir módulos não implementados.")
    now=now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO plans(id,name,status,price_cents,billing_period,created_at) VALUES(?,?,'active',?,?,?)",(data.id,data.name.strip(),decimal_money_cents(data.price) if data.price is not None else None,data.billing_period,now))
            for mid in sorted(set(data.included_modules)): conn.execute("INSERT INTO plan_entitlements(plan_id,module_id,included) VALUES(?,?,1)",(data.id,mid))
    except sqlite3.IntegrityError: raise HTTPException(409,"Plano já existe.")
    return {"id":data.id,"name":data.name.strip(),"status":"active","included_modules":sorted(set(data.included_modules))}

@app.post("/v1/master/companies/{company_id}/subscription")
def master_assign_subscription(company_id: str,data: SubscriptionAssignIn,conn: DB,bootstrap_key: Annotated[str | None,Header(alias="X-Bootstrap-Key")]=None):
    if not BOOTSTRAP_SECRET or not bootstrap_key or not hmac.compare_digest(bootstrap_key,BOOTSTRAP_SECRET): raise HTTPException(403,"Acesso administrativo não autorizado.")
    if not conn.execute("SELECT 1 FROM companies WHERE id=?",(company_id,)).fetchone(): raise HTTPException(404,"Empresa não encontrada.")
    if not conn.execute("SELECT 1 FROM plans WHERE id=? AND status='active'",(data.plan_id,)).fetchone(): raise HTTPException(404,"Plano ativo não encontrado.")
    owner=conn.execute("SELECT id FROM users WHERE company_id=? AND role='owner' ORDER BY created_at LIMIT 1",(company_id,)).fetchone();sid,now=new_id(),now_iso()
    with transaction(conn):
        conn.execute("INSERT INTO company_subscriptions VALUES(?,?,?,?,?,?,?,?)",(sid,company_id,data.plan_id,"pending",data.starts_at,data.ends_at,owner["id"],now))
        audit(conn,company_id,owner["id"],"subscription.pending","subscription",sid,{"plan_id":data.plan_id})
    return {"id":sid,"plan_id":data.plan_id,"status":"pending","entitlements_enabled":False}

def require_restaurant_floor(conn,user):
    profile=conn.execute("SELECT primary_segment,secondary_segments_json FROM company_profiles WHERE company_id=?",(user["company_id"],)).fetchone()
    if not profile or "restaurant" not in [profile["primary_segment"],*json.loads(profile["secondary_segments_json"])]:
        raise HTTPException(409,"Inclua Restaurantes e lanchonetes no perfil do negócio.")
    require_permission(conn,user,"restaurant.floor")
    feat=conn.execute("SELECT available FROM feature_catalog WHERE id='restaurant_floor'").fetchone()
    if not feat or not feat["available"]: raise HTTPException(409,"Módulo de salão indisponível.")

@app.get("/v1/restaurant/areas")
def restaurant_areas(conn: DB,user: USER):
    require_restaurant_floor(conn,user)
    rows=conn.execute("SELECT id,name,active FROM restaurant_areas WHERE company_id=? ORDER BY name",(user["company_id"],)).fetchall()
    return {"items":[{"id":r["id"],"name":r["name"],"active":bool(r["active"])} for r in rows]}

@app.get("/v1/restaurant/floor")
def restaurant_floor(conn: DB,user: USER):
    require_restaurant_floor(conn,user)
    rows=conn.execute("SELECT t.id,t.label,t.capacity,t.status,a.name AS area,s.id AS service_id FROM restaurant_tables t JOIN restaurant_areas a ON a.id=t.area_id LEFT JOIN restaurant_services s ON s.table_id=t.id AND s.company_id=t.company_id AND s.status='open' WHERE t.company_id=? AND t.active=1 ORDER BY a.name,t.label",(user["company_id"],)).fetchall()
    return {"items":[dict(r) for r in rows],"total":len(rows)}

@app.post("/v1/restaurant/areas",status_code=201)
def restaurant_create_area(data: RestaurantAreaIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager");require_restaurant_floor(conn,user)
    rid,now=new_id(),now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO restaurant_areas(id,company_id,name,active,created_by,created_at) VALUES(?,?,?,1,?,?)",(rid,user["company_id"],data.name.strip(),user["id"],now))
            audit(conn,user["company_id"],user["id"],"restaurant.area_created","restaurant_area",rid,{"name":data.name.strip()})
    except sqlite3.IntegrityError: raise HTTPException(409,"Ambiente já cadastrado.")
    return {"id":rid,"name":data.name.strip(),"active":True}

@app.post("/v1/restaurant/tables",status_code=201)
def restaurant_create_table(data: RestaurantTableIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager");require_restaurant_floor(conn,user)
    if not conn.execute("SELECT 1 FROM restaurant_areas WHERE id=? AND company_id=? AND active=1",(data.area_id,user["company_id"])).fetchone(): raise HTTPException(404,"Ambiente não encontrado.")
    tid,now=new_id(),now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO restaurant_tables(id,company_id,area_id,label,capacity,status,active,created_by,created_at) VALUES(?,?,?,?,?,'available',1,?,?)",(tid,user["company_id"],data.area_id,data.label.strip(),data.capacity,user["id"],now))
            audit(conn,user["company_id"],user["id"],"restaurant.table_created","restaurant_table",tid,{"area_id":data.area_id,"label":data.label.strip(),"capacity":data.capacity})
    except sqlite3.IntegrityError: raise HTTPException(409,"Mesa já cadastrada.")
    return {"id":tid,"area_id":data.area_id,"label":data.label.strip(),"capacity":data.capacity,"status":"available"}

@app.post("/v1/restaurant/tables/{table_id}/open")
def restaurant_open_table(table_id: str,conn: DB,user: USER):
    require_role(user,"owner","admin","manager","operator");require_restaurant_floor(conn,user)
    sid,now=new_id(),now_iso()
    try:
        with transaction(conn):
            table=conn.execute("SELECT status FROM restaurant_tables WHERE id=? AND company_id=? AND active=1",(table_id,user["company_id"])).fetchone()
            if not table: raise HTTPException(404,"Mesa não encontrada.")
            if table["status"]!='available': raise HTTPException(409,"Mesa não está livre.")
            conn.execute("INSERT INTO restaurant_services(id,company_id,table_id,status,opened_by,opened_at,closed_at) VALUES(?,?,?,'open',?,?,NULL)",(sid,user["company_id"],table_id,user["id"],now))
            updated=conn.execute("UPDATE restaurant_tables SET status='occupied' WHERE id=? AND company_id=? AND status='available'",(table_id,user["company_id"]))
            if updated.rowcount!=1: raise HTTPException(409,"Mesa ocupada por outra operação.")
            audit(conn,user["company_id"],user["id"],"restaurant.service_opened","restaurant_service",sid,{"table_id":table_id})
    except sqlite3.IntegrityError: raise HTTPException(409,"Já existe atendimento para essa mesa.")
    except Exception as exc:
        if getattr(exc,"pgcode",None) in {"40001","40P01","23505"} or (isinstance(exc,sqlite3.OperationalError) and "locked" in str(exc).lower()):
            raise HTTPException(409,"A mesa foi ocupada por outra operação concorrente; atualize o salão.")
        raise
    return {"id":sid,"table_id":table_id,"status":"open","opened_at":now}

@app.get("/v1/restaurant/reservations")
def restaurant_list_reservations(conn: DB,user: USER,status_filter: Literal["confirmed","seated","cancelled","completed","all"]="confirmed"):
    require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.floor")
    rows=conn.execute("SELECT r.id,r.table_id,t.label AS table_label,a.name AS area,r.customer_name,r.party_size,r.starts_at,r.ends_at,r.status FROM restaurant_reservations r JOIN restaurant_tables t ON t.id=r.table_id AND t.company_id=r.company_id JOIN restaurant_areas a ON a.id=t.area_id AND a.company_id=t.company_id WHERE r.company_id=? AND (?='all' OR r.status=?) ORDER BY r.starts_at LIMIT 200",(user["company_id"],status_filter,status_filter)).fetchall()
    return {"items":[dict(r) for r in rows]}

@app.post("/v1/restaurant/reservations",status_code=201)
def restaurant_reserve(data: RestaurantReservationIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager","operator");require_restaurant_floor(conn,user)
    if data.ends_at<=data.starts_at: raise HTTPException(422,"Fim da reserva deve ser depois do início.")
    table=conn.execute("SELECT capacity FROM restaurant_tables WHERE id=? AND company_id=? AND active=1",(data.table_id,user["company_id"])).fetchone()
    if not table: raise HTTPException(404,"Mesa não encontrada.")
    if data.party_size>table["capacity"]: raise HTTPException(409,"Grupo excede a capacidade da mesa.")
    rid,now=new_id(),now_iso()
    with transaction(conn):
        if conn.execute("SELECT 1 FROM restaurant_reservations WHERE company_id=? AND table_id=? AND status IN ('confirmed','seated') AND starts_at<? AND ends_at>?",(user["company_id"],data.table_id,data.ends_at,data.starts_at)).fetchone(): raise HTTPException(409,"Reserva conflitante.")
        conn.execute("INSERT INTO restaurant_reservations(id,company_id,table_id,customer_name,party_size,starts_at,ends_at,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,'confirmed',?,?)",(rid,user["company_id"],data.table_id,data.customer_name.strip(),data.party_size,data.starts_at,data.ends_at,user["id"],now))
        audit(conn,user["company_id"],user["id"],"restaurant.reservation_created","restaurant_reservation",rid,{"table_id":data.table_id,"starts_at":data.starts_at,"ends_at":data.ends_at})
    return {"id":rid,"table_id":data.table_id,"status":"confirmed","starts_at":data.starts_at,"ends_at":data.ends_at}

@app.get("/v1/restaurant/menu")
def restaurant_menu(conn: DB,user: USER):
    require_restaurant_floor(conn,user)
    cats=conn.execute("SELECT id,name FROM restaurant_menu_categories WHERE company_id=? AND active=1 ORDER BY name",(user["company_id"],)).fetchall()
    rows=conn.execute("SELECT m.id,m.category_id,c.name AS category,m.product_id,p.name,p.price_cents,p.unit,m.station FROM restaurant_menu_items m JOIN restaurant_menu_categories c ON c.id=m.category_id AND c.company_id=m.company_id JOIN products p ON p.id=m.product_id AND p.company_id=m.company_id WHERE m.company_id=? AND m.active=1 AND p.active=1 ORDER BY c.name,p.name",(user["company_id"],)).fetchall()
    return {"categories":[dict(x) for x in cats],"items":[{"id":r["id"],"category_id":r["category_id"],"category":r["category"],"product_id":r["product_id"],"name":r["name"],"price":cents_to_money(r["price_cents"]),"unit":r["unit"],"station":r["station"]} for r in rows]}

@app.post("/v1/restaurant/menu/categories",status_code=201)
def restaurant_create_category(data: RestaurantCategoryIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager");require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.menu")
    cid,now=new_id(),now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO restaurant_menu_categories(id,company_id,name,active,created_by,created_at) VALUES(?,?,?,1,?,?)",(cid,user["company_id"],data.name.strip(),user["id"],now))
            audit(conn,user["company_id"],user["id"],"restaurant.menu_category_created","restaurant_menu_category",cid,{"name":data.name.strip()})
    except sqlite3.IntegrityError: raise HTTPException(409,"Categoria já cadastrada.")
    return {"id":cid,"name":data.name.strip()}

@app.post("/v1/restaurant/menu/items",status_code=201)
def restaurant_create_menu_item(data: RestaurantMenuItemIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager");require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.menu")
    if not conn.execute("SELECT 1 FROM restaurant_menu_categories WHERE id=? AND company_id=? AND active=1",(data.category_id,user["company_id"])).fetchone(): raise HTTPException(404,"Categoria não encontrada.")
    if not conn.execute("SELECT 1 FROM products WHERE id=? AND company_id=? AND active=1",(data.product_id,user["company_id"])).fetchone(): raise HTTPException(404,"Produto do estoque central não encontrado.")
    mid,now=new_id(),now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO restaurant_menu_items(id,company_id,category_id,product_id,station,active,created_by,created_at) VALUES(?,?,?,?,?,1,?,?)",(mid,user["company_id"],data.category_id,data.product_id,data.station,user["id"],now))
            audit(conn,user["company_id"],user["id"],"restaurant.menu_item_created","restaurant_menu_item",mid,{"product_id":data.product_id,"category_id":data.category_id,"station":data.station})
    except sqlite3.IntegrityError: raise HTTPException(409,"Produto já vinculado ao cardápio.")
    return {"id":mid,"category_id":data.category_id,"product_id":data.product_id,"station":data.station}

@app.post("/v1/restaurant/services/{service_id}/tabs",status_code=201)
def restaurant_create_tab(service_id: str,data: RestaurantTabIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager","operator");require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.orders")
    service=conn.execute("SELECT id FROM restaurant_services WHERE id=? AND company_id=? AND status='open'",(service_id,user["company_id"])).fetchone()
    if not service: raise HTTPException(404,"Atendimento aberto não encontrado.")
    tid,now=new_id(),now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO restaurant_tabs(id,company_id,service_id,label,status,opened_by,opened_at,closed_at) VALUES(?,?,?,?,'open',?,?,NULL)",(tid,user["company_id"],service_id,data.label.strip(),user["id"],now))
            audit(conn,user["company_id"],user["id"],"restaurant.tab_opened","restaurant_tab",tid,{"service_id":service_id,"label":data.label.strip()})
    except sqlite3.IntegrityError: raise HTTPException(409,"Comanda já existe para este atendimento ou nome repetido.")
    return {"id":tid,"service_id":service_id,"label":data.label.strip(),"status":"open"}

@app.post("/v1/restaurant/tabs/{tab_id}/orders",status_code=201)
def restaurant_create_order(tab_id: str,data: RestaurantOrderIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager","operator");require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.orders")
    oid,now=new_id(),now_iso()
    with transaction(conn):
        tab=conn.execute("SELECT id,status FROM restaurant_tabs WHERE id=? AND company_id=?",(tab_id,user["company_id"])).fetchone()
        if not tab or tab["status"]!='open': raise HTTPException(409,"Comanda não está aberta.")
        conn.execute("INSERT INTO restaurant_orders(id,company_id,tab_id,status,notes,created_by,created_at,updated_at) VALUES(?,?,?,'open',?,?,?,?)",(oid,user["company_id"],tab_id,data.notes.strip(),user["id"],now,now))
        for line in data.items:
            menu=conn.execute("SELECT m.id,m.product_id,m.station,p.name,p.price_cents FROM restaurant_menu_items m JOIN products p ON p.id=m.product_id AND p.company_id=m.company_id WHERE m.id=? AND m.company_id=? AND m.active=1 AND p.active=1",(line.menu_item_id,user["company_id"])).fetchone()
            if not menu: raise HTTPException(404,"Item de cardápio não encontrado.")
            conn.execute("INSERT INTO restaurant_order_items(id,company_id,order_id,menu_item_id,product_id,name_snapshot,quantity_milli,unit_price_cents,station,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,'queued',?)",(new_id(),user["company_id"],oid,menu["id"],menu["product_id"],menu["name"],decimal_milli(line.quantity),menu["price_cents"],menu["station"],now))
        audit(conn,user["company_id"],user["id"],"restaurant.order_created","restaurant_order",oid,{"tab_id":tab_id,"item_count":len(data.items),"notes":data.notes})
        conn.execute("INSERT INTO restaurant_events(id,company_id,tab_id,entity_type,entity_id,action,details_json,actor_id,created_at) VALUES(?,?,?,?,?,?,?, ?,?)",(new_id(),user["company_id"],tab_id,"order",oid,"created",json.dumps({"items":[x.menu_item_id for x in data.items],"notes":data.notes}),user["id"],now))
    return {"id":oid,"tab_id":tab_id,"status":"open","items":len(data.items)}

@app.post("/v1/restaurant/orders/{order_id}/send")
def restaurant_send_order(order_id: str,conn: DB,user: USER):
    require_role(user,"owner","admin","manager","operator");require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.orders")
    now=now_iso()
    with transaction(conn):
        order=conn.execute("SELECT * FROM restaurant_orders WHERE id=? AND company_id=?",(order_id,user["company_id"])).fetchone()
        if not order: raise HTTPException(404,"Pedido não encontrado.")
        if order["status"]!='open': raise HTTPException(409,"Pedido já foi enviado ou mudou de estado.")
        conn.execute("UPDATE restaurant_orders SET status='sent',updated_at=? WHERE id=? AND company_id=?",(now,order_id,user["company_id"]))
        audit(conn,user["company_id"],user["id"],"restaurant.order_sent","restaurant_order",order_id,{"tab_id":order["tab_id"]})
        conn.execute("INSERT INTO restaurant_events(id,company_id,tab_id,entity_type,entity_id,action,details_json,actor_id,created_at) VALUES(?,?,?,?,?,?,?, ?,?)",(new_id(),user["company_id"],order["tab_id"],"order",order_id,"sent",json.dumps({}),user["id"],now))
    return {"id":order_id,"status":"sent","sent_at":now}

@app.get("/v1/restaurant/production")
def restaurant_production(station: Literal["kitchen","bar","counter"],conn: DB,user: USER):
    require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.production")
    rows=conn.execute("SELECT i.id,i.order_id,i.product_id,i.name_snapshot,i.quantity_milli,i.status,i.station,o.tab_id,o.created_at FROM restaurant_order_items i JOIN restaurant_orders o ON o.id=i.order_id AND o.company_id=i.company_id JOIN restaurant_tabs t ON t.id=o.tab_id AND t.company_id=o.company_id WHERE i.company_id=? AND i.station=? AND i.status IN ('queued','preparing','ready') AND o.status IN ('sent','preparing','ready') AND t.status='open' ORDER BY o.created_at,i.created_at",(user["company_id"],station)).fetchall()
    return {"station":station,"items":[{"id":r["id"],"order_id":r["order_id"],"tab_id":r["tab_id"],"product_id":r["product_id"],"name":r["name_snapshot"],"quantity":milli_to_decimal(r["quantity_milli"]),"status":r["status"],"created_at":r["created_at"]} for r in rows]}

@app.patch("/v1/restaurant/order-items/{item_id}")
def restaurant_update_item(item_id: str,data: RestaurantItemStatusIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager","operator");require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.production")
    now=now_iso()
    with transaction(conn):
        row=conn.execute("SELECT i.*,o.tab_id,o.status AS order_status,t.status AS tab_status FROM restaurant_order_items i JOIN restaurant_orders o ON o.id=i.order_id AND o.company_id=i.company_id JOIN restaurant_tabs t ON t.id=o.tab_id AND t.company_id=o.company_id WHERE i.id=? AND i.company_id=?",(item_id,user["company_id"])).fetchone()
        if not row: raise HTTPException(404,"Item do pedido não encontrado.")
        allowed={"queued":{"preparing","cancelled"},"preparing":{"ready","cancelled"},"ready":{"delivered"}}
        if data.status not in allowed.get(row["status"],set()) or row["tab_status"]!='open': raise HTTPException(409,"Transição de produção inválida.")
        conn.execute("UPDATE restaurant_order_items SET status=? WHERE id=? AND company_id=?",(data.status,item_id,user["company_id"]))
        items=conn.execute("SELECT status FROM restaurant_order_items WHERE order_id=? AND company_id=?",(row["order_id"],user["company_id"])).fetchall()
        states={x["status"] for x in items}
        order_status="delivered" if states<={"delivered","cancelled"} and "delivered" in states else "ready" if states<={"ready","delivered","cancelled"} and "ready" in states else "preparing" if "preparing" in states else row["order_status"]
        conn.execute("UPDATE restaurant_orders SET status=?,updated_at=? WHERE id=? AND company_id=?",(order_status,now,row["order_id"],user["company_id"]))
        conn.execute("INSERT INTO restaurant_events(id,company_id,tab_id,entity_type,entity_id,action,details_json,actor_id,created_at) VALUES(?,?,?,?,?,?,?, ?,?)",(new_id(),user["company_id"],row["tab_id"],"order_item",item_id,data.status,json.dumps({"from":row["status"],"to":data.status}),user["id"],now))
        audit(conn,user["company_id"],user["id"],"restaurant.order_item_status","restaurant_order_item",item_id,{"from":row["status"],"to":data.status,"order_id":row["order_id"]})
    return {"id":item_id,"status":data.status,"order_status":order_status}

@app.get("/v1/restaurant/tabs")
def restaurant_list_tabs(conn: DB,user: USER,status_filter: Literal["open","closed","all"]="open"):
    require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.orders")
    rows=conn.execute("SELECT t.id,t.service_id,t.label,t.status,t.opened_at,s.table_id,rt.label AS table_label,a.name AS area FROM restaurant_tabs t JOIN restaurant_services s ON s.id=t.service_id AND s.company_id=t.company_id JOIN restaurant_tables rt ON rt.id=s.table_id AND rt.company_id=s.company_id JOIN restaurant_areas a ON a.id=rt.area_id AND a.company_id=rt.company_id WHERE t.company_id=? AND (?='all' OR t.status=?) ORDER BY t.opened_at DESC LIMIT 100",(user["company_id"],status_filter,status_filter)).fetchall()
    return {"items":[dict(r) for r in rows]}

@app.get("/v1/restaurant/tabs/{tab_id}")
def restaurant_tab_detail(tab_id: str,conn: DB,user: USER):
    require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.orders")
    tab=conn.execute("SELECT t.*,s.table_id FROM restaurant_tabs t JOIN restaurant_services s ON s.id=t.service_id AND s.company_id=t.company_id WHERE t.id=? AND t.company_id=?",(tab_id,user["company_id"])).fetchone()
    if not tab: raise HTTPException(404,"Comanda não encontrada.")
    items=conn.execute("SELECT i.id,i.order_id,i.product_id,i.name_snapshot,i.quantity_milli,i.unit_price_cents,i.station,i.status FROM restaurant_order_items i JOIN restaurant_orders o ON o.id=i.order_id AND o.company_id=i.company_id WHERE o.tab_id=? AND i.company_id=? AND i.status<>'cancelled' ORDER BY o.created_at,i.created_at",(tab_id,user["company_id"])).fetchall()
    payments=conn.execute("SELECT amount_cents,method,received_at FROM restaurant_tab_payments WHERE tab_id=? AND company_id=? ORDER BY received_at",(tab_id,user["company_id"])).fetchall()
    total=sum((r["quantity_milli"]*r["unit_price_cents"]+500)//1000 for r in items);paid=sum(r["amount_cents"] for r in payments)
    return {"id":tab["id"],"label":tab["label"],"status":tab["status"],"table_id":tab["table_id"],"items":[{"id":r["id"],"order_id":r["order_id"],"product_id":r["product_id"],"name":r["name_snapshot"],"quantity":milli_to_decimal(r["quantity_milli"]),"unit_price":cents_to_money(r["unit_price_cents"]),"station":r["station"],"status":r["status"],"line_total":cents_to_money((r["quantity_milli"]*r["unit_price_cents"]+500)//1000)} for r in items],"total":cents_to_money(total),"paid":cents_to_money(paid),"remaining":cents_to_money(max(0,total-paid)),"payments":[{"amount":cents_to_money(r["amount_cents"]),"method":r["method"],"received_at":r["received_at"]} for r in payments]}

@app.post("/v1/restaurant/tabs/{tab_id}/payments",status_code=201)
def restaurant_tab_cash_payment(tab_id: str,data: RestaurantCashPaymentIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager","operator");require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.cash")
    amount=decimal_money_cents(data.amount);received=decimal_money_cents(data.received_amount)
    if received<amount: raise HTTPException(422,"Dinheiro recebido insuficiente.")
    now=now_iso()
    with transaction(conn):
        prior=conn.execute("SELECT id,tab_id,amount_cents,received_at FROM restaurant_tab_payments WHERE company_id=? AND idempotency_key=?",(user["company_id"],data.idempotency_key)).fetchone()
        if prior:
            if prior["tab_id"]!=tab_id or prior["amount_cents"]!=amount: raise HTTPException(409,"Chave idempotente reutilizada com dados diferentes.")
            return {"tab_id":tab_id,"amount":cents_to_money(prior["amount_cents"]),"change":cents_to_money(received-amount),"method":"cash","status":"already_recorded","idempotent":True,"received_at":prior["received_at"],"electronic_confirmation":False}
        tab=conn.execute("SELECT t.*,s.table_id FROM restaurant_tabs t JOIN restaurant_services s ON s.id=t.service_id AND s.company_id=t.company_id WHERE t.id=? AND t.company_id=?",(tab_id,user["company_id"])).fetchone()
        if not tab or tab["status"]!='open': raise HTTPException(409,"Comanda não está aberta.")
        row=conn.execute("SELECT COALESCE(SUM((i.quantity_milli*i.unit_price_cents+500)/1000),0) FROM restaurant_order_items i JOIN restaurant_orders o ON o.id=i.order_id AND o.company_id=i.company_id WHERE o.tab_id=? AND i.company_id=? AND i.status<>'cancelled'",(tab_id,user["company_id"])).fetchone()
        total=int(row[0]);paid=int(conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM restaurant_tab_payments WHERE tab_id=? AND company_id=?",(tab_id,user["company_id"])).fetchone()[0])
        if paid+amount>total: raise HTTPException(409,"Recebimento excede o saldo da comanda.")
        # Only cash self-declaration is supported; require a real open central register and record that receipt there.
        register=conn.execute("SELECT * FROM cash_registers WHERE company_id=? AND status='open' ORDER BY opened_at DESC LIMIT 1",(user["company_id"],)).fetchone()
        if not register: raise HTTPException(409,"Abra um caixa central antes de receber a comanda.")
        orders=conn.execute("SELECT DISTINCT o.id,o.status FROM restaurant_orders o JOIN restaurant_order_items i ON i.order_id=o.id AND i.company_id=o.company_id WHERE o.tab_id=? AND o.company_id=? AND i.status<>'cancelled'",(tab_id,user["company_id"])).fetchall()
        if any(o["status"]!="delivered" for o in orders): raise HTTPException(409,"Todos os itens devem ser entregues antes de receber.")
        conn.execute("INSERT INTO restaurant_tab_payments(id,company_id,tab_id,amount_cents,method,idempotency_key,received_by,received_at) VALUES(?,?,?,?,'cash',?,?,?)",(new_id(),user["company_id"],tab_id,amount,data.idempotency_key,user["id"],now))
        conn.execute("INSERT INTO cash_movements(id,company_id,register_id,actor_id,kind,amount_cents,reason,sale_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(new_id(),user["company_id"],register["id"],user["id"],"sale",amount,"Comanda "+tab_id,None,now))
        if paid+amount==total:
            sale_id=new_id()
            request_hash=hashlib.sha256(json.dumps({"restaurant_tab":tab_id,"total_cents":total},sort_keys=True).encode()).hexdigest()
            conn.execute("INSERT INTO sales(id,company_id,register_id,created_by,idempotency_key,request_hash,status,subtotal_cents,total_cents,payment_method,received_cents,change_cents,created_at,customer_id) VALUES(?,?,?,?,?,?,'paid',?,?,'cash',?,0,?,NULL)",(sale_id,user["company_id"],register["id"],user["id"],"restaurant-tab:"+tab_id,request_hash,total,total,total,now))
            for line in conn.execute("SELECT i.product_id,i.name_snapshot,i.quantity_milli,i.unit_price_cents,p.sku FROM restaurant_order_items i JOIN restaurant_orders o ON o.id=i.order_id AND o.company_id=i.company_id JOIN products p ON p.id=i.product_id AND p.company_id=i.company_id WHERE o.tab_id=? AND i.company_id=? AND i.status='delivered'",(tab_id,user["company_id"])).fetchall():
                gross=(line["quantity_milli"]*line["unit_price_cents"]+500)//1000
                conn.execute("INSERT INTO sale_items(id,sale_id,product_id,sku,name,quantity_milli,unit_price_cents,discount_cents,line_total_cents) VALUES(?,?,?,?,?,?,?,?,?)",(new_id(),sale_id,line["product_id"],line["sku"],line["name_snapshot"],line["quantity_milli"],line["unit_price_cents"],0,gross))
            for item in conn.execute("SELECT product_id,SUM(quantity_milli) AS qty FROM restaurant_order_items i JOIN restaurant_orders o ON o.id=i.order_id AND o.company_id=i.company_id WHERE o.tab_id=? AND i.company_id=? AND i.status='delivered' GROUP BY product_id",(tab_id,user["company_id"])).fetchall():
                product=conn.execute("SELECT * FROM products WHERE id=? AND company_id=? AND active=1",(item["product_id"],user["company_id"])).fetchone()
                if not product or product["stock_milli"]<item["qty"]: raise HTTPException(409,"Estoque insuficiente para concluir a comanda.")
                after=product["stock_milli"]-item["qty"]
                conn.execute("UPDATE products SET stock_milli=? WHERE id=? AND company_id=?",(after,product["id"],user["company_id"]))
                conn.execute("INSERT INTO stock_movements(id,company_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,sale_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(new_id(),user["company_id"],product["id"],user["id"],"sale",-item["qty"],product["stock_milli"],after,"Baixa comanda "+tab_id,None,now))
            audit(conn,user["company_id"],user["id"],"sale.completed","sale",sale_id,{"total":cents_to_money(total),"payment_method":"cash","register_id":register["id"],"source":"restaurant_tab","tab_id":tab_id})
            conn.execute("UPDATE restaurant_tabs SET status='closed',closed_at=? WHERE id=? AND company_id=?",(now,tab_id,user["company_id"]))
            service_id=tab["service_id"]
            open_tabs=conn.execute("SELECT 1 FROM restaurant_tabs WHERE service_id=? AND company_id=? AND status='open'",(service_id,user["company_id"])).fetchone()
            if not open_tabs:
                conn.execute("UPDATE restaurant_services SET status='closed',closed_at=? WHERE id=? AND company_id=?",(now,service_id,user["company_id"]))
                conn.execute("UPDATE restaurant_tables SET status='available' WHERE id=? AND company_id=?",(tab["table_id"],user["company_id"]))
        audit(conn,user["company_id"],user["id"],"restaurant.cash_payment","restaurant_tab",tab_id,{"amount":cents_to_money(amount),"method":"cash","fully_paid":paid+amount==total})
        conn.execute("INSERT INTO restaurant_events(id,company_id,tab_id,entity_type,entity_id,action,details_json,actor_id,created_at) VALUES(?,?,?,?,?,?,?, ?,?)",(new_id(),user["company_id"],tab_id,"payment",data.idempotency_key,"cash_received",json.dumps({"amount_cents":amount,"change_cents":received-amount}),user["id"],now))
    return {"tab_id":tab_id,"amount":cents_to_money(amount),"change":cents_to_money(received-amount),"method":"cash","status":"closed" if paid+amount==total else "partially_paid","electronic_confirmation":False}

@app.get("/v1/restaurant/reports/operations")
def restaurant_operations_report(conn: DB,user: USER):
    require_restaurant_floor(conn,user);require_permission(conn,user,"restaurant.orders")
    open_tabs=conn.execute("SELECT count(*) FROM restaurant_tabs WHERE company_id=? AND status='open'",(user["company_id"],)).fetchone()[0]
    open_tables=conn.execute("SELECT count(*) FROM restaurant_tables WHERE company_id=? AND active=1 AND status='occupied'",(user["company_id"],)).fetchone()[0]
    order_counts={r["status"]:r["n"] for r in conn.execute("SELECT status,count(*) AS n FROM restaurant_orders WHERE company_id=? GROUP BY status",(user["company_id"],)).fetchall()}
    cash=conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM restaurant_tab_payments WHERE company_id=?",(user["company_id"],)).fetchone()[0]
    return {"open_tabs":open_tabs,"occupied_tables":open_tables,"orders_by_status":order_counts,"cash_received":cents_to_money(cash),"payment_methods_available":["cash"],"electronic_payments":"unavailable","fiscal":"unavailable"}

@app.get("/v1/company/commercial")
def company_commercial(conn: DB,user: USER):
    sub=conn.execute("SELECT s.id,s.plan_id,s.status,s.starts_at,s.ends_at,p.name AS plan_name FROM company_subscriptions s JOIN plans p ON p.id=s.plan_id WHERE s.company_id=? ORDER BY s.created_at DESC LIMIT 1",(user["company_id"],)).fetchone()
    rows=conn.execute("SELECT module_id,source,status FROM company_module_entitlements WHERE company_id=? ORDER BY module_id,source",(user["company_id"],)).fetchall()
    return {"subscription":dict(sub) if sub else None,"entitlements":[dict(r) for r in rows],"commercial_configuration":"unassigned" if not sub else "assigned"}

@app.get("/v1/users")
def list_users(conn: DB, user: USER):
    require_role(user, "owner", "admin", "manager")
    rows = conn.execute("SELECT id,email,full_name,role,active,created_at FROM users WHERE company_id=? ORDER BY full_name", (user["company_id"],)).fetchall()
    return {"items": [{"id": row["id"], "email": row["email"], "name": row["full_name"], "role": row["role"], "active": bool(row["active"]), "created_at": row["created_at"]} for row in rows]}


@app.post("/v1/users", status_code=201)
def create_user(data: UserCreateIn, conn: DB, user: USER):
    require_role(user, "owner", "admin")
    if user["role"] == "admin" and data.role == "admin":
        raise HTTPException(403, "Somente o titular pode criar outro administrador.")
    user_id, created = new_id(), now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?,1,?)", (user_id, user["company_id"], data.email, data.full_name.strip(), password_hash(data.password), data.role, created))
            primary=conn.execute("SELECT id FROM branches WHERE company_id=? AND code='main' AND status='active'",(user["company_id"],)).fetchone()
            if primary: conn.execute("INSERT INTO branch_users(company_id,branch_id,user_id) VALUES(?,?,?)",(user["company_id"],primary["id"],user_id))
            audit(conn, user["company_id"], user["id"], "user.created", "user", user_id, {"email": data.email, "role": data.role})
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Já existe um usuário com esse e-mail nesta empresa.")
    return {"id": user_id, "company_id": user["company_id"], "email": data.email, "name": data.full_name.strip(), "role": data.role, "active": True}


@app.get("/v1/customers")
def list_customers(conn: DB,user: USER,search: str=Query(default="",max_length=100),limit: int=Query(default=100,ge=1,le=200)):
    require_role(user,"owner","admin","manager","operator")
    query=f"%{search.strip()}%"
    if user["role"]=="operator":
        # PDV lookup is name-only and returns no contact, tax or notes data.
        rows=conn.execute("SELECT id,name FROM customers WHERE company_id=? AND active=1 AND (?='' OR name LIKE ?) ORDER BY name LIMIT ?",
            (user["company_id"],search.strip(),query,limit)).fetchall()
        return {"items":[{"id":r["id"],"name":r["name"]} for r in rows]}
    rows=conn.execute("""SELECT * FROM customers WHERE company_id=? AND active=1 AND (?='' OR name LIKE ? OR email LIKE ? OR phone LIKE ? OR COALESCE(tax_id,'') LIKE ?) ORDER BY name LIMIT ?""",
        (user["company_id"],search.strip(),query,query,query,query,limit)).fetchall()
    return {"items":[{"id":r["id"],"name":r["name"],"email":r["email"],"phone":r["phone"],"tax_id":r["tax_id"],"notes":r["notes"],"created_at":r["created_at"]} for r in rows]}


@app.get("/v1/customers/{customer_id}")
def customer_detail(customer_id: str,conn: DB,user: USER):
    require_role(user,"owner","admin","manager")
    customer=conn.execute("SELECT * FROM customers WHERE id=? AND company_id=? AND active=1",(customer_id,user["company_id"])).fetchone()
    if not customer: raise HTTPException(404,"Cliente não encontrado nesta empresa.")
    rows=conn.execute("SELECT id,status,total_cents,payment_method,created_at FROM sales WHERE company_id=? AND customer_id=? ORDER BY created_at DESC LIMIT 100",(user["company_id"],customer_id)).fetchall()
    return {"customer":{"id":customer["id"],"name":customer["name"],"email":customer["email"],"phone":customer["phone"],"tax_id":customer["tax_id"],"notes":customer["notes"],"created_at":customer["created_at"]},
            "sales":{"items":[{"id":r["id"],"status":r["status"],"total":cents_to_money(r["total_cents"]),"payment_method":r["payment_method"],"created_at":r["created_at"]} for r in rows],"count":len(rows),"total_paid":cents_to_money(sum(r["total_cents"] for r in rows if r["status"]=="paid"))}}


@app.post("/v1/customers",status_code=201)
def create_customer(data: CustomerIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager","operator")
    if user["role"]!="operator": require_permission(conn,user,"customers.write")
    if user["role"]=="operator" and any((data.email.strip(),data.phone.strip(),(data.tax_id or "").strip(),data.notes.strip())):
        raise HTTPException(403,"Operador pode cadastrar apenas o nome do cliente nesta versão.")
    customer_id,created=new_id(),now_iso()
    email=data.email.strip().lower()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO customers(id,company_id,name,email,phone,tax_id,notes,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)",(customer_id,user["company_id"],data.name.strip(),email,data.phone.strip(),data.tax_id,data.notes.strip(),created,created))
            audit(conn,user["company_id"],user["id"],"customer.created","customer",customer_id,{"name":data.name.strip(),"email":email,"phone":data.phone.strip()})
    except sqlite3.IntegrityError:
        raise HTTPException(409,"Já existe cliente com esse e-mail nesta empresa.")
    return {"id":customer_id,"name":data.name.strip(),"email":email,"phone":data.phone.strip(),"created_at":created}


@app.get("/v1/suppliers")
def list_suppliers(conn: DB,user: USER,active: bool=True):
    require_role(user,"owner","admin","manager","stock")
    rows=conn.execute("SELECT * FROM suppliers WHERE company_id=? AND active=? ORDER BY name",(user["company_id"],int(active))).fetchall()
    if user["role"]=="stock":
        return {"items":[{"id":r["id"],"name":r["name"],"active":bool(r["active"])} for r in rows]}
    return {"items":[{"id":r["id"],"name":r["name"],"tax_id":r["tax_id"],"email":r["email"],"phone":r["phone"],"notes":r["notes"],"active":bool(r["active"]),"created_at":r["created_at"]} for r in rows]}


@app.post("/v1/suppliers",status_code=201)
def create_supplier(data: SupplierIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager","stock")
    require_permission(conn,user,"purchases.manage")
    supplier_id,created=new_id(),now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO suppliers(id,company_id,name,tax_id,email,phone,notes,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)",(supplier_id,user["company_id"],data.name,data.tax_id,data.email,data.phone,data.notes,created,created))
            audit(conn,user["company_id"],user["id"],"supplier.created","supplier",supplier_id,{"name":data.name})
    except sqlite3.IntegrityError:
        raise HTTPException(409,"Já existe um fornecedor com esse nome nesta empresa.")
    return {"id":supplier_id,"name":data.name,"active":True,"created_at":created}


@app.post("/v1/branches/{branch_id}/purchase-orders",status_code=201)
def create_branch_purchase_order(branch_id: str,data: PurchaseCreateIn,conn: DB,user: USER):
    require_permission(conn,user,"purchases.manage");require_branch_membership(conn,user,branch_id)
    order_id,created=new_id(),now_iso()
    try:
        with transaction(conn):
            supplier=conn.execute("SELECT id FROM suppliers WHERE id=? AND company_id=? AND active=1",(data.supplier_id,user["company_id"])).fetchone()
            if not supplier:raise HTTPException(404,"Fornecedor não encontrado nesta empresa.")
            conn.execute("INSERT INTO purchase_orders(id,company_id,supplier_id,order_number,status,notes,created_by,created_at,updated_at,branch_id) VALUES(?,?,?,?,'draft',?,?,?,?,?)",(order_id,user["company_id"],data.supplier_id,data.order_number.strip(),data.notes.strip(),user["id"],created,created,branch_id))
            for line in data.items:
                product=company_product(conn,user["company_id"],line.product_id)
                conn.execute("INSERT INTO purchase_order_items(id,purchase_order_id,company_id,product_id,ordered_milli,received_milli,unit_cost_cents,branch_id) VALUES(?,?,?,?,?,0,?,?)",(new_id(),order_id,user["company_id"],product["id"],decimal_milli(line.quantity),decimal_money_cents(line.unit_cost),branch_id))
            audit(conn,user["company_id"],user["id"],"branch_purchase_order.created","purchase_order",order_id,{"branch_id":branch_id,"supplier_id":data.supplier_id,"order_number":data.order_number,"line_count":len(data.items)})
    except sqlite3.IntegrityError:raise HTTPException(409,"Número de pedido já cadastrado ou item inválido.")
    return {"id":order_id,"branch_id":branch_id,"order_number":data.order_number.strip(),"status":"draft","created_at":created}

@app.get("/v1/branches/{branch_id}/purchase-orders")
def list_branch_purchase_orders(branch_id: str,conn: DB,user: USER,limit: int=Query(default=100,ge=1,le=200)):
    require_permission(conn,user,"purchases.manage");require_branch_membership(conn,user,branch_id)
    rows=conn.execute("SELECT po.*,s.name AS supplier_name FROM purchase_orders po JOIN suppliers s ON s.id=po.supplier_id AND s.company_id=po.company_id WHERE po.company_id=? AND po.branch_id=? ORDER BY po.created_at DESC LIMIT ?",(user["company_id"],branch_id,limit)).fetchall();items=[]
    for row in rows:
        lines=conn.execute("SELECT poi.id,poi.product_id,p.name,p.sku,poi.ordered_milli,poi.received_milli,poi.unit_cost_cents FROM purchase_order_items poi JOIN products p ON p.id=poi.product_id AND p.company_id=poi.company_id WHERE poi.company_id=? AND poi.purchase_order_id=? AND poi.branch_id=? ORDER BY p.name",(user["company_id"],row["id"],branch_id)).fetchall()
        items.append({"id":row["id"],"branch_id":branch_id,"supplier_id":row["supplier_id"],"supplier":row["supplier_name"],"order_number":row["order_number"],"status":row["status"],"notes":row["notes"],"created_at":row["created_at"],"items":[{"id":i["id"],"product_id":i["product_id"],"name":i["name"],"sku":i["sku"],"ordered":milli_to_decimal(i["ordered_milli"]),"received":milli_to_decimal(i["received_milli"]),"unit_cost":cents_to_money(i["unit_cost_cents"])} for i in lines]})
    return {"items":items}

@app.patch("/v1/branches/{branch_id}/purchase-orders/{order_id}")
def update_branch_purchase_status(branch_id: str,order_id: str,data: PurchaseStatusIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager");require_permission(conn,user,"purchases.manage");require_branch_membership(conn,user,branch_id)
    with transaction(conn):
        order=conn.execute("SELECT * FROM purchase_orders WHERE id=? AND company_id=? AND branch_id=?",(order_id,user["company_id"],branch_id)).fetchone()
        if not order:raise HTTPException(404,"Pedido não encontrado nesta filial.")
        if order["status"] not in {"draft","ordered"}:raise HTTPException(409,"Este pedido não pode mudar de estado.")
        if data.status=="cancelled" and conn.execute("SELECT 1 FROM purchase_order_items WHERE purchase_order_id=? AND company_id=? AND branch_id=? AND received_milli>0",(order_id,user["company_id"],branch_id)).fetchone():raise HTTPException(409,"Pedido com recebimento parcial não pode ser cancelado; finalize recebimento primeiro.")
        conn.execute("UPDATE purchase_orders SET status=?,updated_at=? WHERE id=? AND company_id=? AND branch_id=?",(data.status,now_iso(),order_id,user["company_id"],branch_id))
        audit(conn,user["company_id"],user["id"],"branch_purchase_order.status_changed","purchase_order",order_id,{"branch_id":branch_id,"before":order["status"],"after":data.status})
    return {"id":order_id,"branch_id":branch_id,"status":data.status}

@app.post("/v1/branches/{branch_id}/purchase-orders/{order_id}/receipts",status_code=201)
def receive_branch_purchase(branch_id: str,order_id: str,data: PurchaseReceiveIn,request: Request,conn: DB,user: USER,
                            idempotency_key: Annotated[str|None,Header(alias="Idempotency-Key")]=None,response: Response=None):
    require_permission(conn,user,"purchases.manage");require_branch_membership(conn,user,branch_id)
    if not idempotency_key or len(idempotency_key)>128:raise HTTPException(400,"Envie Idempotency-Key para evitar recebimento duplicado.")
    normalized={"branch_id":branch_id,"items":sorted([{"item":x.order_item_id,"quantity":str(x.quantity)} for x in data.items],key=lambda x:x["item"]),"due_date":data.due_date.isoformat() if data.due_date else None}
    request_hash=hashlib.sha256(json.dumps(normalized,sort_keys=True,separators=(",",":")).encode()).hexdigest();receipt_id,now=new_id(),now_iso()
    with transaction(conn):
        order=conn.execute("SELECT * FROM purchase_orders WHERE id=? AND company_id=? AND branch_id=?",(order_id,user["company_id"],branch_id)).fetchone()
        if not order:raise HTTPException(404,"Pedido não encontrado nesta filial.")
        prior=conn.execute("SELECT * FROM purchase_receipts WHERE company_id=? AND idempotency_key=?",(user["company_id"],idempotency_key)).fetchone()
        if prior:
            if prior["request_hash"]!=request_hash or prior["purchase_order_id"]!=order_id:raise HTTPException(409,"Chave de idempotência reutilizada com dados diferentes.")
            if response is not None:response.status_code=200
            return {"receipt_id":prior["id"],"branch_id":branch_id,"status":"already_received","idempotent":True}
        if order["status"] not in {"ordered","partially_received"}:raise HTTPException(409,"Pedido precisa estar enviado e aberto para recebimento.")
        if len({x.order_item_id for x in data.items})!=len(data.items):raise HTTPException(422,"Não repita a mesma linha de pedido no recebimento.")
        receipt_rows=[]
        for line in data.items:
            qty=decimal_milli(line.quantity)
            item=conn.execute("SELECT poi.*,p.name,p.sku FROM purchase_order_items poi JOIN products p ON p.id=poi.product_id AND p.company_id=poi.company_id WHERE poi.id=? AND poi.purchase_order_id=? AND poi.company_id=? AND poi.branch_id=?",(line.order_item_id,order_id,user["company_id"],branch_id)).fetchone()
            if not item:raise HTTPException(404,"Linha do pedido não encontrada nesta filial.")
            if item["received_milli"]+qty>item["ordered_milli"]:raise HTTPException(409,"Quantidade recebida supera o saldo do pedido.")
            receipt_rows.append((line,item,qty))
        if data.due_date is not None and data.due_date<date.today():raise HTTPException(422,"Vencimento não pode ser anterior à data de hoje.")
        conn.execute("INSERT INTO purchase_receipts(id,company_id,purchase_order_id,received_by,received_at,idempotency_key,request_hash,branch_id) VALUES(?,?,?,?,?,?,?,?)",(receipt_id,user["company_id"],order_id,user["id"],now,idempotency_key,request_hash,branch_id))
        payable_amount=0
        for line,item,qty in receipt_rows:
            row=conn.execute("SELECT quantity_milli FROM branch_product_stock WHERE company_id=? AND branch_id=? AND product_id=?",(user["company_id"],branch_id,item["product_id"])).fetchone();before=int(row[0]) if row else 0;after=before+qty
            conn.execute("INSERT INTO branch_product_stock(company_id,branch_id,product_id,quantity_milli,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(company_id,branch_id,product_id) DO UPDATE SET quantity_milli=excluded.quantity_milli,updated_at=excluded.updated_at",(user["company_id"],branch_id,item["product_id"],after,now))
            conn.execute("UPDATE purchase_order_items SET received_milli=received_milli+? WHERE id=? AND company_id=? AND branch_id=?",(qty,line.order_item_id,user["company_id"],branch_id))
            conn.execute("INSERT INTO purchase_receipt_items(id,receipt_id,purchase_order_item_id,product_id,quantity_milli,unit_cost_cents,branch_id) VALUES(?,?,?,?,?,?,?)",(new_id(),receipt_id,line.order_item_id,item["product_id"],qty,item["unit_cost_cents"],branch_id))
            conn.execute("INSERT INTO branch_stock_events(id,company_id,branch_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,transfer_id,created_at) VALUES(?,?,?,?,?,'adjustment_in',?,?,?,?,NULL,?)",(new_id(),user["company_id"],branch_id,item["product_id"],user["id"],qty,before,after,"Recebimento de pedido "+order["order_number"],now))
            payable_amount+=(qty*item["unit_cost_cents"]+500)//1000
        remaining=conn.execute("SELECT COUNT(*) FROM purchase_order_items WHERE purchase_order_id=? AND company_id=? AND branch_id=? AND received_milli<ordered_milli",(order_id,user["company_id"],branch_id)).fetchone()[0];new_status="received" if remaining==0 else "partially_received"
        payable_id=None
        if data.due_date is not None:
            payable_id=new_id();conn.execute("INSERT INTO accounts_payable(id,company_id,supplier_id,source_receipt_id,description,amount_cents,due_date,status,created_by,created_at,branch_id) VALUES(?,?,?,?,?,?,?,'open',?,?,?)",(payable_id,user["company_id"],order["supplier_id"],receipt_id,"Recebimento pedido "+order["order_number"],payable_amount,data.due_date.isoformat(),user["id"],now,branch_id))
            audit(conn,user["company_id"],user["id"],"branch_payable.created_from_receipt","accounts_payable",payable_id,{"branch_id":branch_id,"receipt_id":receipt_id,"amount":cents_to_money(payable_amount),"due_date":data.due_date.isoformat()})
        conn.execute("UPDATE purchase_orders SET status=?,updated_at=? WHERE id=? AND company_id=? AND branch_id=?",(new_status,now,order_id,user["company_id"],branch_id))
        audit(conn,user["company_id"],user["id"],"branch_purchase.received","purchase_order",order_id,{"branch_id":branch_id,"receipt_id":receipt_id,"payable_id":payable_id,"status":new_status})
    return {"receipt_id":receipt_id,"branch_id":branch_id,"payable_id":payable_id,"payable_amount":cents_to_money(payable_amount) if payable_id else None,"due_date":data.due_date.isoformat() if payable_id else None,"status":new_status,"received_at":now,"idempotent":False}

@app.get("/v1/branches/{branch_id}/finance/summary")
def branch_finance_summary(branch_id: str,conn: DB,user: USER):
    require_permission(conn,user,"finance.read");require_branch_membership(conn,user,branch_id)
    sales=conn.execute("SELECT COALESCE(SUM(total_cents),0) FROM branch_sales WHERE company_id=? AND branch_id=? AND status='paid'",(user["company_id"],branch_id)).fetchone()[0]
    open_total=conn.execute("SELECT COALESCE(SUM(CASE WHEN amount_cents-COALESCE((SELECT SUM(pp.amount_cents) FROM payable_payments pp WHERE pp.company_id=ap.company_id AND pp.payable_id=ap.id),0)>0 THEN amount_cents-COALESCE((SELECT SUM(pp.amount_cents) FROM payable_payments pp WHERE pp.company_id=ap.company_id AND pp.payable_id=ap.id),0) ELSE 0 END),0) FROM accounts_payable ap WHERE company_id=? AND branch_id=? AND status='open'",(user["company_id"],branch_id)).fetchone()[0]
    overdue=conn.execute("SELECT COALESCE(SUM(CASE WHEN amount_cents-COALESCE((SELECT SUM(pp.amount_cents) FROM payable_payments pp WHERE pp.company_id=ap.company_id AND pp.payable_id=ap.id),0)>0 THEN amount_cents-COALESCE((SELECT SUM(pp.amount_cents) FROM payable_payments pp WHERE pp.company_id=ap.company_id AND pp.payable_id=ap.id),0) ELSE 0 END),0) FROM accounts_payable ap WHERE company_id=? AND branch_id=? AND status='open' AND due_date<?",(user["company_id"],branch_id,date.today().isoformat())).fetchone()[0]
    return {"branch_id":branch_id,"cash_sales_total":cents_to_money(sales),"open_payables":cents_to_money(open_total),"overdue_payables":cents_to_money(overdue),"note":"Dados isolados da filial; baixas manuais não são confirmação bancária."}

@app.get("/v1/branches/{branch_id}/finance/cashflow")
def branch_finance_cashflow(branch_id: str,conn: DB,user: USER,start_date: date=Query(...),end_date: date=Query(...)):
    require_permission(conn,user,"finance.read");require_branch_membership(conn,user,branch_id)
    if end_date<start_date or (end_date-start_date).days>365:raise HTTPException(422,"Período inválido (máximo 366 dias).")
    start,end=start_date.isoformat(),end_date.isoformat()
    sales=conn.execute("SELECT substr(created_at,1,10) AS day,SUM(total_cents) AS cents,COUNT(*) AS count FROM branch_sales WHERE company_id=? AND branch_id=? AND status='paid' AND substr(created_at,1,10) BETWEEN ? AND ? GROUP BY day",(user["company_id"],branch_id,start,end)).fetchall()
    pays=conn.execute("SELECT pp.payment_date AS day,SUM(pp.amount_cents) AS cents,COUNT(*) AS count FROM payable_payments pp JOIN accounts_payable ap ON ap.id=pp.payable_id AND ap.company_id=pp.company_id WHERE pp.company_id=? AND ap.branch_id=? AND pp.payment_date BETWEEN ? AND ? GROUP BY pp.payment_date",(user["company_id"],branch_id,start,end)).fetchall()
    a={r["day"]:{"amount":cents_to_money(r["cents"]),"count":r["count"]} for r in sales};b={r["day"]:{"amount":cents_to_money(r["cents"]),"count":r["count"]} for r in pays}
    return {"branch_id":branch_id,"start_date":start,"end_date":end,"items":[{"date":d,"cash_sales":a.get(d,{"amount":"0.00","count":0}),"manual_payments_out":b.get(d,{"amount":"0.00","count":0})} for d in sorted(set(a)|set(b))],"note":"Movimentos da filial; saídas são baixas manuais não conciliadas."}

@app.post("/v1/branches/{branch_id}/finance/expenses",status_code=201)
def create_branch_expense(branch_id: str,data: ExpenseIn,conn: DB,user: USER,idempotency_key: Annotated[str|None,Header(alias="Idempotency-Key")]=None,response: Response=None):
    require_role(user,"owner","admin","manager");require_permission(conn,user,"finance.write");require_branch_membership(conn,user,branch_id)
    if not idempotency_key or len(idempotency_key)>128:raise HTTPException(400,"Envie Idempotency-Key para evitar duplicidade.")
    cents=decimal_money_cents(data.amount)
    if data.due_date<date.today():raise HTTPException(422,"Vencimento não pode ser anterior à data de hoje.")
    if data.payment_date and data.payment_date>date.today():raise HTTPException(422,"Data de pagamento não pode ser futura.")
    body={"branch_id":branch_id,"description":data.description.strip(),"amount":cents,"due_date":data.due_date.isoformat(),"cost_center":data.cost_center.strip(),"payment_date":data.payment_date.isoformat() if data.payment_date else None,"payment_reference":data.payment_reference.strip()}
    digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest();pid,now=new_id(),now_iso()
    with transaction(conn):
        prior=conn.execute("SELECT ed.payable_id,ed.request_hash,ap.status,ap.amount_cents,ap.due_date FROM payable_expense_details ed JOIN accounts_payable ap ON ap.id=ed.payable_id AND ap.company_id=ed.company_id WHERE ed.company_id=? AND ed.idempotency_key=?",(user["company_id"],idempotency_key)).fetchone()
        if prior:
            if prior["request_hash"]!=digest:raise HTTPException(409,"Idempotency-Key reutilizada com outros dados.")
            if response is not None:response.status_code=200
            return {"id":prior["payable_id"],"branch_id":branch_id,"status":prior["status"],"amount":cents_to_money(prior["amount_cents"]),"idempotent":True}
        conn.execute("INSERT INTO accounts_payable(id,company_id,supplier_id,source_receipt_id,description,amount_cents,due_date,status,created_by,created_at,branch_id) VALUES(?,?,NULL,NULL,?,?,?,'open',?,?,?)",(pid,user["company_id"],data.description.strip(),cents,data.due_date.isoformat(),user["id"],now,branch_id))
        conn.execute("INSERT INTO payable_expense_details(payable_id,company_id,cost_center,idempotency_key,request_hash) VALUES(?,?,?,?,?)",(pid,user["company_id"],data.cost_center.strip(),idempotency_key,digest))
        status="open"
        if data.payment_date:
            payid=new_id();conn.execute("INSERT INTO payable_payments(id,company_id,payable_id,amount_cents,payment_date,reference,idempotency_key,created_by,created_at,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)",(payid,user["company_id"],pid,cents,data.payment_date.isoformat(),data.payment_reference.strip(),"expense-"+payid,user["id"],now,branch_id))
            conn.execute("UPDATE accounts_payable SET status='paid',paid_at=?,payment_note=? WHERE id=? AND company_id=? AND branch_id=?",(now,data.payment_reference.strip(),pid,user["company_id"],branch_id));status="paid"
        audit(conn,user["company_id"],user["id"],"branch_expense.created","accounts_payable",pid,{"branch_id":branch_id,"description":data.description.strip(),"amount":cents_to_money(cents),"due_date":data.due_date.isoformat(),"status":status,"manual_payment":bool(data.payment_date)})
    return {"id":pid,"branch_id":branch_id,"status":status,"amount":cents_to_money(cents),"due_date":data.due_date.isoformat(),"idempotent":False,"note":"Despesa da filial registrada; baixas manuais não confirmam pagamento bancário."}

@app.get("/v1/branches/{branch_id}/payables")
def list_branch_payables(branch_id: str,conn: DB,user: USER,status_filter: str=Query(default="all",alias="status"),limit: int=Query(default=100,ge=1,le=200)):
    require_permission(conn,user,"finance.read");require_branch_membership(conn,user,branch_id)
    if status_filter not in {"all","open","paid","cancelled"}:raise HTTPException(422,"Filtro de situação inválido.")
    rows=conn.execute("SELECT ap.*,s.name AS supplier_name,COALESCE((SELECT SUM(pp.amount_cents) FROM payable_payments pp WHERE pp.company_id=ap.company_id AND pp.payable_id=ap.id),0) AS paid_cents FROM accounts_payable ap LEFT JOIN suppliers s ON s.id=ap.supplier_id AND s.company_id=ap.company_id WHERE ap.company_id=? AND ap.branch_id=? AND (?='all' OR ap.status=?) ORDER BY ap.due_date,ap.created_at DESC LIMIT ?",(user["company_id"],branch_id,status_filter,status_filter,limit)).fetchall()
    return {"items":[{"id":r["id"],"branch_id":branch_id,"supplier_id":r["supplier_id"],"supplier":r["supplier_name"],"description":r["description"],"amount":cents_to_money(r["amount_cents"]),"paid":cents_to_money(r["paid_cents"]),"remaining":cents_to_money(max(0,r["amount_cents"]-r["paid_cents"])),"due_date":r["due_date"],"status":r["status"],"created_at":r["created_at"],"paid_at":r["paid_at"]} for r in rows]}

@app.post("/v1/branches/{branch_id}/payables/{payable_id}/payments",status_code=201)
def pay_branch_payable(branch_id: str,payable_id: str,data: PayablePartialIn,conn: DB,user: USER,
                       idempotency_key: Annotated[str|None,Header(alias="Idempotency-Key")]=None,response: Response=None):
    require_role(user,"owner","admin","manager");require_permission(conn,user,"finance.write");require_branch_membership(conn,user,branch_id)
    if not idempotency_key or len(idempotency_key)>128:raise HTTPException(400,"Envie Idempotency-Key para registrar a baixa sem duplicidade.")
    amount=decimal_money_cents(data.amount)
    if amount<=0:raise HTTPException(422,"Valor deve ser de ao menos R$ 0,01.")
    if data.payment_date>date.today():raise HTTPException(422,"Data de pagamento não pode ser futura.")
    now=now_iso()
    with transaction(conn):
        prior=conn.execute("SELECT * FROM payable_payments WHERE company_id=? AND idempotency_key=?",(user["company_id"],idempotency_key)).fetchone()
        if prior:
            if prior["payable_id"]!=payable_id or prior["amount_cents"]!=amount or prior["reference"]!=data.reference or prior["payment_date"]!=data.payment_date.isoformat():raise HTTPException(409,"Chave de idempotência reutilizada com outros dados.")
            payable=conn.execute("SELECT amount_cents,status FROM accounts_payable WHERE id=? AND company_id=? AND branch_id=?",(payable_id,user["company_id"],branch_id)).fetchone()
            if not payable:raise HTTPException(404,"Conta não encontrada nesta filial.")
            paid=conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payable_payments WHERE company_id=? AND payable_id=?",(user["company_id"],payable_id)).fetchone()[0]
            if response is not None:response.status_code=200
            return {"id":prior["id"],"branch_id":branch_id,"status":payable["status"],"paid":cents_to_money(paid),"remaining":cents_to_money(max(0,payable["amount_cents"]-paid)),"idempotent":True,"note":"Baixa manual; não confirmada por banco."}
        payable=conn.execute("SELECT * FROM accounts_payable WHERE id=? AND company_id=? AND branch_id=?",(payable_id,user["company_id"],branch_id)).fetchone()
        if not payable:raise HTTPException(404,"Conta não encontrada nesta filial.")
        if payable["status"]!="open":raise HTTPException(409,"Conta não está em aberto.")
        paid=conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payable_payments WHERE company_id=? AND payable_id=?",(user["company_id"],payable_id)).fetchone()[0];remaining=payable["amount_cents"]-paid
        if amount>remaining:raise HTTPException(409,"Pagamento supera o saldo restante.")
        pid=new_id();conn.execute("INSERT INTO payable_payments(id,company_id,payable_id,amount_cents,payment_date,reference,idempotency_key,created_by,created_at,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)",(pid,user["company_id"],payable_id,amount,data.payment_date.isoformat(),data.reference,idempotency_key,user["id"],now,branch_id))
        updated=paid+amount;status="paid" if updated==payable["amount_cents"] else "open"
        if status=="paid":conn.execute("UPDATE accounts_payable SET status='paid',paid_at=?,payment_note=? WHERE id=? AND company_id=? AND branch_id=?",(now,data.reference,payable_id,user["company_id"],branch_id))
        audit(conn,user["company_id"],user["id"],"branch_payable.payment_recorded","payable_payment",pid,{"branch_id":branch_id,"payable_id":payable_id,"amount":cents_to_money(amount),"status":"manual_not_bank_confirmed"})
    return {"id":pid,"branch_id":branch_id,"status":status,"paid":cents_to_money(updated),"remaining":cents_to_money(max(0,payable["amount_cents"]-updated)),"idempotent":False,"note":"Baixa manual; não confirmada por banco."}

@app.post("/v1/purchase-orders",status_code=201)
def create_purchase_order(data: PurchaseCreateIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager","stock")
    require_permission(conn,user,"purchases.manage")
    order_id,created=new_id(),now_iso()
    try:
        with transaction(conn):
            supplier=conn.execute("SELECT id FROM suppliers WHERE id=? AND company_id=? AND active=1",(data.supplier_id,user["company_id"])).fetchone()
            if not supplier: raise HTTPException(404,"Fornecedor não encontrado nesta empresa.")
            conn.execute("INSERT INTO purchase_orders(id,company_id,supplier_id,order_number,status,notes,created_by,created_at,updated_at) VALUES(?,?,?,?,'draft',?,?,?,?)",(order_id,user["company_id"],data.supplier_id,data.order_number.strip(),data.notes.strip(),user["id"],created,created))
            for line in data.items:
                product=company_product(conn,user["company_id"],line.product_id)
                conn.execute("INSERT INTO purchase_order_items(id,purchase_order_id,company_id,product_id,ordered_milli,received_milli,unit_cost_cents) VALUES(?,?,?,?,?,0,?)",(new_id(),order_id,user["company_id"],product["id"],decimal_milli(line.quantity),decimal_money_cents(line.unit_cost)))
            audit(conn,user["company_id"],user["id"],"purchase_order.created","purchase_order",order_id,{"supplier_id":data.supplier_id,"order_number":data.order_number,"line_count":len(data.items)})
    except sqlite3.IntegrityError:
        raise HTTPException(409,"Número de pedido já cadastrado ou item inválido.")
    return {"id":order_id,"order_number":data.order_number.strip(),"status":"draft","created_at":created}


@app.get("/v1/finance/payables")
def list_payables(conn: DB,user: USER,status_filter: str=Query(default="all",alias="status"),limit: int=Query(default=100,ge=1,le=200)):
    require_role(user,"owner","admin","manager")
    require_permission(conn,user,"finance.read")
    if status_filter not in {"all","open","paid","cancelled"}: raise HTTPException(422,"Filtro de situação inválido.")
    rows=conn.execute("""SELECT ap.*,s.name AS supplier_name,
        COALESCE((SELECT SUM(pp.amount_cents) FROM payable_payments pp WHERE pp.company_id=ap.company_id AND pp.payable_id=ap.id),0) AS paid_cents,
        COALESCE(ed.cost_center,'') AS cost_center
        FROM accounts_payable ap LEFT JOIN suppliers s ON s.id=ap.supplier_id
        LEFT JOIN payable_expense_details ed ON ed.payable_id=ap.id AND ed.company_id=ap.company_id
        WHERE ap.company_id=? AND (?='all' OR ap.status=?) ORDER BY ap.due_date,ap.created_at DESC LIMIT ?""",
        (user["company_id"],status_filter,status_filter,limit)).fetchall()
    return {"items":[{"id":r["id"],"supplier_id":r["supplier_id"],"supplier":r["supplier_name"],"source_receipt_id":r["source_receipt_id"],"description":r["description"],"amount":cents_to_money(r["amount_cents"]),"paid":cents_to_money(r["paid_cents"]),"remaining":cents_to_money(max(0,r["amount_cents"]-r["paid_cents"])),"cost_center":r["cost_center"],"due_date":r["due_date"],"status":r["status"],"created_at":r["created_at"],"paid_at":r["paid_at"],"payment_note":r["payment_note"]} for r in rows]}


@app.post("/v1/finance/payables/{payable_id}/mark-paid")
def mark_payable_paid(payable_id: str,data: PayablePaidIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager")
    require_permission(conn,user,"finance.write")
    paid_at=now_iso()
    with transaction(conn):
        row=conn.execute("SELECT * FROM accounts_payable WHERE id=? AND company_id=?",(payable_id,user["company_id"])).fetchone()
        if not row: raise HTTPException(404,"Conta a pagar não encontrada nesta empresa.")
        if row["status"]!="open": raise HTTPException(409,"A conta não está em aberto.")
        already=conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payable_payments WHERE company_id=? AND payable_id=?",(user["company_id"],payable_id)).fetchone()[0]
        remaining=row["amount_cents"]-already
        if remaining<=0: raise HTTPException(409,"Conta sem saldo pendente.")
        payment_id=new_id()
        conn.execute("INSERT INTO payable_payments(id,company_id,payable_id,amount_cents,payment_date,reference,idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(payment_id,user["company_id"],payable_id,remaining,date.today().isoformat(),data.payment_note,"legacy-full-"+payment_id,user["id"],paid_at))
        conn.execute("UPDATE accounts_payable SET status='paid',paid_at=?,payment_note=? WHERE id=? AND company_id=?",(paid_at,data.payment_note,payable_id,user["company_id"]))
        audit(conn,user["company_id"],user["id"],"payable.manually_marked_paid","accounts_payable",payable_id,{"amount":cents_to_money(remaining),"payment_note":data.payment_note,"warning":"Baixa declarada manualmente; não confirmada por integração bancária."})
    return {"id":payable_id,"status":"paid","paid_at":paid_at,"note":"Baixa manual registrada; não representa confirmação bancária."}


@app.post("/v1/finance/payables/{payable_id}/payments",status_code=201)
def record_payable_payment(payable_id: str,data: PayablePartialIn,request: Request,conn: DB,user: USER,
                           idempotency_key: Annotated[str|None,Header(alias="Idempotency-Key")]=None,response: Response=None):
    require_role(user,"owner","admin","manager")
    require_permission(conn,user,"finance.write")
    if not idempotency_key or not 1<=len(idempotency_key)<=128:
        raise HTTPException(400,"Envie Idempotency-Key para registrar a baixa sem duplicidade.")
    amount_cents=decimal_money_cents(data.amount)
    if amount_cents<=0: raise HTTPException(422,"Valor deve ser de ao menos R$ 0,01.")
    if data.payment_date>date.today(): raise HTTPException(422,"Data de pagamento não pode ser futura.")
    created=now_iso()
    with transaction(conn):
        prior=conn.execute("SELECT * FROM payable_payments WHERE company_id=? AND idempotency_key=?",(user["company_id"],idempotency_key)).fetchone()
        if prior:
            if prior["payable_id"]!=payable_id or prior["amount_cents"]!=amount_cents or prior["reference"]!=data.reference or prior["payment_date"]!=data.payment_date.isoformat():
                raise HTTPException(409,"Chave de idempotência reutilizada com dados diferentes.")
            if response is not None: response.status_code=200
            payable=conn.execute("SELECT amount_cents,status FROM accounts_payable WHERE id=? AND company_id=?",(payable_id,user["company_id"])).fetchone()
            paid=conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payable_payments WHERE company_id=? AND payable_id=?",(user["company_id"],payable_id)).fetchone()[0]
            return {"id":prior["id"],"status":payable["status"],"paid":cents_to_money(paid),"remaining":cents_to_money(max(0,payable["amount_cents"]-paid)),"note":"Baixa manual registrada; não confirmada por banco."}
        payable=conn.execute("SELECT * FROM accounts_payable WHERE id=? AND company_id=?",(payable_id,user["company_id"])).fetchone()
        if not payable: raise HTTPException(404,"Conta a pagar não encontrada nesta empresa.")
        if payable["status"]!="open": raise HTTPException(409,"Conta não está em aberto.")
        already=conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payable_payments WHERE company_id=? AND payable_id=?",(user["company_id"],payable_id)).fetchone()[0]
        remaining=payable["amount_cents"]-already
        if amount_cents>remaining: raise HTTPException(409,"O pagamento supera o saldo restante.")
        payment_id=new_id()
        conn.execute("INSERT INTO payable_payments(id,company_id,payable_id,amount_cents,payment_date,reference,idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(payment_id,user["company_id"],payable_id,amount_cents,data.payment_date.isoformat(),data.reference,idempotency_key,user["id"],created))
        paid=already+amount_cents
        new_status="paid" if paid==payable["amount_cents"] else "open"
        if new_status=="paid": conn.execute("UPDATE accounts_payable SET status='paid',paid_at=?,payment_note=? WHERE id=? AND company_id=?",(created,data.reference,payable_id,user["company_id"]))
        audit(conn,user["company_id"],user["id"],"payable.payment_recorded","payable_payment",payment_id,{"payable_id":payable_id,"amount":cents_to_money(amount_cents),"paid_to_date":cents_to_money(paid),"remaining":cents_to_money(payable["amount_cents"]-paid),"payment_date":data.payment_date.isoformat(),"reference":data.reference,"status":"manual_not_bank_confirmed"})
    return {"id":payment_id,"status":new_status,"paid":cents_to_money(paid),"remaining":cents_to_money(payable["amount_cents"]-paid),"note":"Baixa manual registrada; não confirmada por banco."}


@app.post("/v1/finance/expenses",status_code=201)
def create_expense(data: ExpenseIn,conn: DB,user: USER,
                   idempotency_key: Annotated[str|None,Header(alias="Idempotency-Key")]=None,response: Response=None):
    require_role(user,"owner","admin","manager")
    require_permission(conn,user,"finance.write")
    require_permission(conn,user,"finance.write")
    if not idempotency_key or not 1<=len(idempotency_key)<=128:
        raise HTTPException(400,"Envie Idempotency-Key para evitar duplicidade.")
    amount_cents=decimal_money_cents(data.amount)
    if amount_cents<=0: raise HTTPException(422,"Valor deve ser de ao menos R$ 0,01.")
    normalized={"description":data.description,"amount":amount_cents,"due_date":data.due_date.isoformat(),"cost_center":data.cost_center,"payment_date":data.payment_date.isoformat() if data.payment_date else None,"payment_reference":data.payment_reference}
    request_hash=hashlib.sha256(json.dumps(normalized,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    payable_id,created=new_id(),now_iso()
    with transaction(conn):
        prior=conn.execute("SELECT ed.payable_id,ed.request_hash,ap.status,ap.amount_cents,ap.due_date,ed.cost_center FROM payable_expense_details ed JOIN accounts_payable ap ON ap.id=ed.payable_id AND ap.company_id=ed.company_id WHERE ed.company_id=? AND ed.idempotency_key=?",(user["company_id"],idempotency_key)).fetchone()
        if prior:
            if prior["request_hash"]!=request_hash: raise HTTPException(409,"Chave de idempotência reutilizada com dados diferentes.")
            if response is not None: response.status_code=200
            return {"id":prior["payable_id"],"amount":cents_to_money(prior["amount_cents"]),"due_date":prior["due_date"],"status":prior["status"],"cost_center":prior["cost_center"],"note":"Registro já existente; eventuais baixas seguem declaradas manualmente."}
        conn.execute("INSERT INTO accounts_payable(id,company_id,supplier_id,source_receipt_id,description,amount_cents,due_date,status,created_by,created_at) VALUES(?,?,NULL,NULL,?,?,?,'open',?,?)",(payable_id,user["company_id"],data.description.strip(),amount_cents,data.due_date.isoformat(),user["id"],created))
        conn.execute("INSERT INTO payable_expense_details(payable_id,company_id,cost_center,idempotency_key,request_hash) VALUES(?,?,?,?,?)",(payable_id,user["company_id"],data.cost_center.strip(),idempotency_key,request_hash))
        if data.payment_date:
            if data.payment_date>date.today(): raise HTTPException(422,"Data de pagamento não pode ser futura.")
            if len(data.payment_reference.strip())<3: raise HTTPException(422,"Informe referência/justificativa para baixa manual.")
            payment_id=new_id()
            conn.execute("INSERT INTO payable_payments(id,company_id,payable_id,amount_cents,payment_date,reference,idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(payment_id,user["company_id"],payable_id,amount_cents,data.payment_date.isoformat(),data.payment_reference.strip(),"expense-"+payment_id,user["id"],created))
            conn.execute("UPDATE accounts_payable SET status='paid',paid_at=?,payment_note=? WHERE id=? AND company_id=?",(created,data.payment_reference.strip(),payable_id,user["company_id"]))
            audit(conn,user["company_id"],user["id"],"expense.manually_marked_paid","payable_payment",payment_id,{"payable_id":payable_id,"amount":cents_to_money(amount_cents),"cost_center":data.cost_center.strip(),"status":"manual_not_bank_confirmed"})
        audit(conn,user["company_id"],user["id"],"expense.created","accounts_payable",payable_id,{"description":data.description.strip(),"amount":cents_to_money(amount_cents),"due_date":data.due_date.isoformat(),"cost_center":data.cost_center.strip(),"payment_status":"paid_manual" if data.payment_date else "open"})
        expense_status="paid" if data.payment_date else "open"
    return {"id":payable_id,"amount":cents_to_money(amount_cents),"due_date":data.due_date.isoformat(),"status":expense_status,"cost_center":data.cost_center.strip(),"note":"Despesa registrada. Qualquer baixa declarada é manual, não confirmação bancária."}


@app.get("/v1/finance/summary")
def finance_summary(conn: DB,user: USER):
    require_role(user,"owner","admin","manager")
    require_permission(conn,user,"finance.read")
    sales=conn.execute("SELECT COALESCE(SUM(total_cents),0) FROM sales WHERE company_id=? AND status='paid'",(user["company_id"],)).fetchone()[0]
    open_amount=conn.execute("SELECT COALESCE(SUM(MAX(0,ap.amount_cents-COALESCE(p.paid_cents,0))),0) FROM accounts_payable ap LEFT JOIN (SELECT company_id,payable_id,SUM(amount_cents) AS paid_cents FROM payable_payments GROUP BY company_id,payable_id) p ON p.company_id=ap.company_id AND p.payable_id=ap.id WHERE ap.company_id=? AND ap.status='open'",(user["company_id"],)).fetchone()[0]
    overdue=conn.execute("SELECT COALESCE(SUM(MAX(0,ap.amount_cents-COALESCE(p.paid_cents,0))),0) FROM accounts_payable ap LEFT JOIN (SELECT company_id,payable_id,SUM(amount_cents) AS paid_cents FROM payable_payments GROUP BY company_id,payable_id) p ON p.company_id=ap.company_id AND p.payable_id=ap.id WHERE ap.company_id=? AND ap.status='open' AND ap.due_date<date('now')",(user["company_id"],)).fetchone()[0]
    return {"cash_sales_total":cents_to_money(sales),"open_payables":cents_to_money(open_amount),"overdue_payables":cents_to_money(overdue),"note":"Vendas em dinheiro não incluem outras formas; baixas manuais não são conciliação bancária."}


@app.get("/v1/finance/cashflow")
def finance_cashflow(conn: DB,user: USER,start_date: date=Query(...),end_date: date=Query(...)):
    require_role(user,"owner","admin","manager")
    require_permission(conn,user,"finance.read")
    if end_date<start_date: raise HTTPException(422,"Data final deve ser igual ou posterior à inicial.")
    if (end_date-start_date).days>365: raise HTTPException(422,"Período máximo do relatório é 366 dias.")
    start=start_date.isoformat(); end=end_date.isoformat()
    sales=conn.execute("SELECT substr(created_at,1,10) AS day,SUM(total_cents) AS cents,COUNT(*) AS count FROM sales WHERE company_id=? AND status='paid' AND substr(created_at,1,10) BETWEEN ? AND ? GROUP BY day ORDER BY day",(user["company_id"],start,end)).fetchall()
    payments=conn.execute("SELECT pp.payment_date AS day,SUM(pp.amount_cents) AS cents,COUNT(*) AS count FROM payable_payments pp JOIN accounts_payable ap ON ap.id=pp.payable_id AND ap.company_id=pp.company_id WHERE pp.company_id=? AND pp.payment_date BETWEEN ? AND ? GROUP BY pp.payment_date ORDER BY pp.payment_date",(user["company_id"],start,end)).fetchall()
    sale_days={r["day"]:{"amount":cents_to_money(r["cents"]),"count":r["count"]} for r in sales}
    manual_days={r["day"]:{"amount":cents_to_money(r["cents"]),"count":r["count"]} for r in payments}
    return {"start_date":start,"end_date":end,"items":[{"date":day,"cash_sales":sale_days.get(day,{"amount":"0.00","count":0}),"manual_payments_out":{"amount":manual_days.get(day,{"amount":"0.00","count":0})["amount"],"count":manual_days.get(day,{"amount":"0.00","count":0})["count"]}} for day in sorted(set(sale_days)|set(manual_days))],"note":"Vendas pagas em dinheiro registradas no sistema. Saídas são baixas declaradas manualmente; não confirmadas por banco e não equivalem a conciliação."}


@app.get("/v1/purchase-orders")
def list_purchase_orders(conn: DB,user: USER,limit: int=Query(default=100,ge=1,le=200)):
    require_role(user,"owner","admin","manager","stock")
    rows=conn.execute("""SELECT po.*,s.name AS supplier_name FROM purchase_orders po JOIN suppliers s ON s.id=po.supplier_id
        WHERE po.company_id=? ORDER BY po.created_at DESC LIMIT ?""",(user["company_id"],limit)).fetchall()
    result=[]
    for row in rows:
        items=conn.execute("""SELECT poi.id,poi.product_id,p.name,p.sku,poi.ordered_milli,poi.received_milli,poi.unit_cost_cents
            FROM purchase_order_items poi JOIN products p ON p.id=poi.product_id WHERE poi.purchase_order_id=? ORDER BY p.name""",(row["id"],)).fetchall()
        result.append({"id":row["id"],"supplier_id":row["supplier_id"],"supplier":row["supplier_name"],"order_number":row["order_number"],"status":row["status"],"notes":row["notes"],"created_at":row["created_at"],"items":[{"id":i["id"],"product_id":i["product_id"],"name":i["name"],"sku":i["sku"],"ordered":milli_to_decimal(i["ordered_milli"]),"received":milli_to_decimal(i["received_milli"]),"unit_cost":cents_to_money(i["unit_cost_cents"])} for i in items]})
    return {"items":result}


@app.patch("/v1/purchase-orders/{order_id}")
def update_purchase_status(order_id: str,data: PurchaseStatusIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager")
    require_permission(conn,user,"purchases.manage")
    with transaction(conn):
        order=conn.execute("SELECT * FROM purchase_orders WHERE id=? AND company_id=?",(order_id,user["company_id"])).fetchone()
        if not order: raise HTTPException(404,"Pedido não encontrado nesta empresa.")
        if order["status"] not in {"draft","ordered"}: raise HTTPException(409,"Este pedido não pode mudar de estado.")
        if data.status=="cancelled":
            received=conn.execute("SELECT COALESCE(SUM(received_milli),0) FROM purchase_order_items WHERE purchase_order_id=?",(order_id,)).fetchone()[0]
            if received: raise HTTPException(409,"Pedido com recebimento parcial não pode ser cancelado; finalize recebimento primeiro.")
        conn.execute("UPDATE purchase_orders SET status=?,updated_at=? WHERE id=? AND company_id=?",(data.status,now_iso(),order_id,user["company_id"]))
        audit(conn,user["company_id"],user["id"],"purchase_order.status_changed","purchase_order",order_id,{"before":order["status"],"after":data.status})
    return {"id":order_id,"status":data.status}


@app.post("/v1/purchase-orders/{order_id}/receipts",status_code=201)
def receive_purchase(order_id: str,data: PurchaseReceiveIn,request: Request,conn: DB,user: USER,
                     idempotency_key: Annotated[str|None,Header(alias="Idempotency-Key")]=None,response: Response=None):
    require_role(user,"owner","admin","manager","stock")
    if not idempotency_key or len(idempotency_key)>128: raise HTTPException(400,"Envie Idempotency-Key para evitar recebimento duplicado.")
    normalized={"items":sorted([{"item":x.order_item_id,"quantity":str(x.quantity)} for x in data.items],key=lambda x:x["item"]),"due_date":data.due_date.isoformat() if data.due_date else None}
    request_hash=hashlib.sha256(json.dumps(normalized,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    receipt_id,received_at=new_id(),now_iso()
    with transaction(conn):
        order=conn.execute("SELECT * FROM purchase_orders WHERE id=? AND company_id=?",(order_id,user["company_id"])).fetchone()
        if not order: raise HTTPException(404,"Pedido não encontrado nesta empresa.")
        prior=conn.execute("SELECT * FROM purchase_receipts WHERE company_id=? AND idempotency_key=?",(user["company_id"],idempotency_key)).fetchone()
        if prior:
            if prior["request_hash"]!=request_hash or prior["purchase_order_id"]!=order_id: raise HTTPException(409,"Chave de idempotência reutilizada com dados diferentes.")
            if response is not None: response.status_code=200
            return {"receipt_id":prior["id"],"status":"already_received"}
        if order["status"] not in {"ordered","partially_received"}: raise HTTPException(409,"Pedido precisa estar enviado e aberto para recebimento.")
        if len({x.order_item_id for x in data.items})!=len(data.items): raise HTTPException(422,"Não repita a mesma linha de pedido no recebimento.")
        receipt_rows=[]
        for line in data.items:
            qty=decimal_milli(line.quantity)
            item=conn.execute("""SELECT poi.*,p.stock_milli,p.name FROM purchase_order_items poi JOIN products p ON p.id=poi.product_id
                WHERE poi.id=? AND poi.purchase_order_id=? AND poi.company_id=?""",(line.order_item_id,order_id,user["company_id"])).fetchone()
            if not item: raise HTTPException(404,"Linha do pedido não encontrada.")
            if item["received_milli"]+qty>item["ordered_milli"]: raise HTTPException(409,"Quantidade recebida supera o saldo do pedido.")
            receipt_rows.append((line,item,qty))
        conn.execute("INSERT INTO purchase_receipts(id,company_id,purchase_order_id,received_by,received_at,idempotency_key,request_hash) VALUES(?,?,?,?,?,?,?)",(receipt_id,user["company_id"],order_id,user["id"],received_at,idempotency_key,request_hash))
        for line,item,qty in receipt_rows:
            before=item["stock_milli"];after=before+qty
            conn.execute("UPDATE products SET stock_milli=? WHERE id=? AND company_id=?",(after,item["product_id"],user["company_id"]))
            conn.execute("UPDATE purchase_order_items SET received_milli=received_milli+? WHERE id=? AND company_id=?",(qty,line.order_item_id,user["company_id"]))
            conn.execute("INSERT INTO purchase_receipt_items(id,receipt_id,purchase_order_item_id,product_id,quantity_milli,unit_cost_cents) VALUES(?,?,?,?,?,?)",(new_id(),receipt_id,line.order_item_id,item["product_id"],qty,item["unit_cost_cents"]))
            conn.execute("INSERT INTO stock_movements(id,company_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,sale_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(new_id(),user["company_id"],item["product_id"],user["id"],"adjustment_in",qty,before,after,"Recebimento de pedido "+order["order_number"],None,received_at))
        remaining=conn.execute("SELECT COUNT(*) FROM purchase_order_items WHERE purchase_order_id=? AND received_milli<ordered_milli",(order_id,)).fetchone()[0]
        new_status="received" if remaining==0 else "partially_received"
        payable_id=None
        payable_amount=sum((qty*item["unit_cost_cents"]+500)//1000 for _,item,qty in receipt_rows)
        if data.due_date is not None:
            if data.due_date < date.today(): raise HTTPException(422,"Vencimento não pode ser anterior à data de hoje.")
            payable_id=new_id()
            conn.execute("INSERT INTO accounts_payable(id,company_id,supplier_id,source_receipt_id,description,amount_cents,due_date,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,'open',?,?)",
                         (payable_id,user["company_id"],order["supplier_id"],receipt_id,"Recebimento pedido "+order["order_number"],payable_amount,data.due_date.isoformat(),user["id"],received_at))
            audit(conn,user["company_id"],user["id"],"payable.created_from_receipt","accounts_payable",payable_id,{"receipt_id":receipt_id,"amount":cents_to_money(payable_amount),"due_date":data.due_date.isoformat()})
        conn.execute("UPDATE purchase_orders SET status=?,updated_at=? WHERE id=? AND company_id=?",(new_status,received_at,order_id,user["company_id"]))
        audit(conn,user["company_id"],user["id"],"purchase.received","purchase_order",order_id,{"receipt_id":receipt_id,"payable_id":payable_id,"items":[{"item_id":x.order_item_id,"quantity":milli_to_decimal(q)} for x,_,q in receipt_rows],"status":new_status})
    return {"receipt_id":receipt_id,"payable_id":payable_id,"payable_amount":cents_to_money(payable_amount) if payable_id else None,"due_date":data.due_date.isoformat() if payable_id else None,"status":new_status,"received_at":received_at}


@app.post("/v1/products", status_code=201)
def create_product(data: ProductIn, conn: DB, user: USER):
    require_role(user, "owner", "admin", "manager")
    require_permission(conn,user,"products.write")
    product_id, created = new_id(), now_iso()
    try:
        with transaction(conn):
            conn.execute("""INSERT INTO products(id,company_id,sku,name,unit,price_cents,cost_cents,created_at,category,barcode,min_stock_milli)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (product_id, user["company_id"], data.sku.strip(), data.name.strip(), data.unit.strip(),
                decimal_money_cents(data.price), decimal_money_cents(data.cost), created, data.category.strip(),
                data.barcode.strip() if data.barcode else None, decimal_milli(data.minimum_stock)))
            audit(conn, user["company_id"], user["id"], "product.created", "product", product_id,
                  {"sku": data.sku.strip(), "name": data.name.strip(), "category":data.category.strip(),"barcode":data.barcode,
                   "minimum_stock":milli_to_decimal(decimal_milli(data.minimum_stock))})
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Já existe um produto com esse SKU ou código de barras nesta empresa.")
    row = conn.execute("SELECT * FROM products WHERE id=? AND company_id=?", (product_id, user["company_id"])).fetchone()
    return product_json(row)


@app.put("/v1/products/{product_id}")
def update_product(product_id: str, data: ProductIn, conn: DB, user: USER):
    require_role(user,"owner","admin","manager")
    require_permission(conn,user,"products.write")
    try:
        with transaction(conn):
            old=conn.execute("SELECT * FROM products WHERE id=? AND company_id=? AND active=1",(product_id,user["company_id"])).fetchone()
            if not old: raise HTTPException(404,"Produto não encontrado nesta empresa.")
            conn.execute("""UPDATE products SET sku=?,name=?,unit=?,price_cents=?,cost_cents=?,category=?,barcode=?,min_stock_milli=?
                WHERE id=? AND company_id=?""",(data.sku.strip(),data.name.strip(),data.unit.strip(),decimal_money_cents(data.price),
                decimal_money_cents(data.cost),data.category.strip(),data.barcode.strip() if data.barcode else None,
                decimal_milli(data.minimum_stock),product_id,user["company_id"]))
            audit(conn,user["company_id"],user["id"],"product.updated","product",product_id,
                  {"before":{"sku":old["sku"],"name":old["name"],"price":cents_to_money(old["price_cents"]),"category":old["category"],"barcode":old["barcode"],"minimum_stock":milli_to_decimal(old["min_stock_milli"])},
                   "after":{"sku":data.sku.strip(),"name":data.name.strip(),"price":cents_to_money(decimal_money_cents(data.price)),"category":data.category.strip(),"barcode":data.barcode,"minimum_stock":milli_to_decimal(decimal_milli(data.minimum_stock))}})
    except sqlite3.IntegrityError:
        raise HTTPException(409,"Já existe um produto com esse SKU ou código de barras nesta empresa.")
    row=conn.execute("SELECT * FROM products WHERE id=? AND company_id=?",(product_id,user["company_id"])).fetchone()
    return product_json(row)


@app.patch("/v1/users/{user_id}")
def update_user(user_id: str, data: UserStatusIn, conn: DB, user: USER):
    require_role(user, "owner", "admin")
    require_permission(conn,user,"users.manage")
    if user_id == user["id"] and not data.active:
        raise HTTPException(409, "Não é possível desativar a própria conta nesta versão.")
    with transaction(conn):
        target = conn.execute("SELECT id,role,active FROM users WHERE id=? AND company_id=?", (user_id, user["company_id"])).fetchone()
        if not target:
            raise HTTPException(404, "Usuário não encontrado nesta empresa.")
        if target["role"] == "owner":
            raise HTTPException(409, "A conta titular não pode ser desativada neste fluxo.")
        if user["role"] == "admin" and target["role"] == "admin":
            raise HTTPException(403, "Somente o titular pode alterar outro administrador.")
        conn.execute("UPDATE users SET active=? WHERE id=? AND company_id=?", (1 if data.active else 0, user_id, user["company_id"]))
        audit(conn, user["company_id"], user["id"], "user.status_changed", "user", user_id, {"active": data.active})
    return {"id": user_id, "active": data.active}


@app.get("/v1/products")
def list_products(conn: DB, user: USER, search: str = Query(default="", max_length=100),
                  limit: int = Query(default=100, ge=1, le=200), offset: int = Query(default=0, ge=0)):
    term = "%" + search.strip().replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute("""SELECT * FROM products WHERE company_id=? AND active=1 AND (?='' OR name LIKE ? ESCAPE '\\' OR sku LIKE ? ESCAPE '\\' OR COALESCE(barcode,'') LIKE ? ESCAPE '\\')
        ORDER BY name LIMIT ? OFFSET ?""", (user["company_id"], search.strip(), term, term, term, limit, offset)).fetchall()
    count = conn.execute("SELECT count(*) FROM products WHERE company_id=? AND active=1", (user["company_id"],)).fetchone()[0]
    items=[product_json(row) for row in rows]
    if user["role"] in {"operator","stock"}:
        for item in items: item.pop("cost",None)
    return {"items": items, "total": count, "limit": limit, "offset": offset}


@app.get("/v1/products/low-stock")
def low_stock_products(conn: DB, user: USER, limit: int = Query(default=100, ge=1, le=200)):
    rows=conn.execute("SELECT * FROM products WHERE company_id=? AND active=1 AND min_stock_milli>0 AND stock_milli<=min_stock_milli ORDER BY (min_stock_milli-stock_milli) DESC LIMIT ?",
                      (user["company_id"],limit)).fetchall()
    count=conn.execute("SELECT count(*) FROM products WHERE company_id=? AND active=1 AND min_stock_milli>0 AND stock_milli<=min_stock_milli",(user["company_id"],)).fetchone()[0]
    return {"items":[product_json(row) for row in rows],"total":count,"limit":limit}


@app.post("/v1/products/{product_id}/stock-adjustments", status_code=201)
def adjust_stock(product_id: str, data: StockAdjustmentIn, conn: DB, user: USER):
    require_role(user, "owner", "admin", "manager", "stock")
    require_permission(conn,user,"stock.adjust")
    quantity = decimal_milli(data.quantity)
    movement_id, created = new_id(), now_iso()
    with transaction(conn):
        product = company_product(conn, user["company_id"], product_id)
        before = product["stock_milli"]
        after = before + quantity if data.direction == "in" else before - quantity
        if after < 0:
            raise HTTPException(409, "Saldo insuficiente para essa baixa de estoque.")
        conn.execute("UPDATE products SET stock_milli=? WHERE id=? AND company_id=?", (after, product_id, user["company_id"]))
        kind = "adjustment_in" if data.direction == "in" else "adjustment_out"
        signed_quantity = quantity if data.direction == "in" else -quantity
        conn.execute("INSERT INTO stock_movements(id,company_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,sale_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (movement_id, user["company_id"], product_id, user["id"], kind, signed_quantity,
                      before, after, data.reason, None, created))
        audit(conn, user["company_id"], user["id"], "stock.adjusted", "product", product_id,
              {"direction": data.direction, "quantity": milli_to_decimal(quantity), "before": milli_to_decimal(before),
               "after": milli_to_decimal(after), "reason": data.reason})
    return {"movement_id": movement_id, "product_id": product_id, "before": milli_to_decimal(before),
            "after": milli_to_decimal(after), "reason": data.reason}


@app.get("/v1/stock-movements")
def list_stock_movements(conn: DB, user: USER, limit: int = Query(default=100, ge=1, le=200), offset: int = Query(default=0,ge=0),
                        product_id: str | None = None, kind: str | None = None):
    allowed={"adjustment_in","adjustment_out","sale","reversal"}
    if kind and kind not in allowed: raise HTTPException(422,"Tipo de movimentação inválido.")
    clauses=["m.company_id=?"];params=[user["company_id"]]
    if product_id: clauses.append("m.product_id=?");params.append(product_id)
    if kind: clauses.append("m.kind=?");params.append(kind)
    where=" AND ".join(clauses)
    rows = conn.execute(f"""SELECT m.*,p.sku,p.name,u.full_name AS actor_name FROM stock_movements m
        JOIN products p ON p.id=m.product_id JOIN users u ON u.id=m.actor_id WHERE {where}
        ORDER BY m.created_at DESC,m.id DESC LIMIT ? OFFSET ?""", (*params,limit,offset)).fetchall()
    count=conn.execute(f"SELECT count(*) FROM stock_movements m WHERE {where}",params).fetchone()[0]
    return {"items": [{"id": r["id"], "product_id": r["product_id"], "sku": r["sku"], "product": r["name"],
                       "actor": r["actor_name"], "kind": r["kind"], "quantity": milli_to_decimal(abs(r["quantity_milli"])),
                       "before": milli_to_decimal(r["before_milli"]), "after": milli_to_decimal(r["after_milli"]),
                       "reason": r["reason"], "sale_id": r["sale_id"], "created_at": r["created_at"]} for r in rows],"total":count,"limit":limit,"offset":offset}


@app.post("/v1/registers/open", status_code=201)
def open_register(data: RegisterOpenIn, conn: DB, user: USER):
    require_role(user, "owner", "admin", "manager", "operator")
    require_permission(conn,user,"cash.manage")
    register_id, opened_at = new_id(), now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO cash_registers(id,company_id,label,status,opening_cents,opened_by,opened_at) VALUES(?,?,?,'open',?,?,?)",
                         (register_id, user["company_id"], data.label.strip(), decimal_money_cents(data.opening_amount), user["id"], opened_at))
            audit(conn, user["company_id"], user["id"], "register.opened", "register", register_id,
                  {"label": data.label.strip(), "opening_amount": cents_to_money(decimal_money_cents(data.opening_amount))})
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Já existe um caixa aberto com esse nome.")
    row = conn.execute("SELECT * FROM cash_registers WHERE id=?", (register_id,)).fetchone()
    return register_json(row)


@app.get("/v1/registers/current")
def current_register(conn: DB, user: USER):
    require_role(user,"owner","admin","manager","operator")
    row = conn.execute("SELECT * FROM cash_registers WHERE company_id=? AND status='open' ORDER BY opened_at DESC LIMIT 1", (user["company_id"],)).fetchone()
    if not row:
        return {"register": None}
    movements = conn.execute("SELECT kind,amount_cents FROM cash_movements WHERE register_id=?", (row["id"],)).fetchall()
    expected = row["opening_cents"] + sum(m["amount_cents"] for m in movements)
    return {"register": register_json(row), "expected_amount": cents_to_money(expected)}


@app.post("/v1/registers/{register_id}/movements", status_code=201)
def cash_movement(register_id: str, data: CashMovementIn, conn: DB, user: USER):
    require_role(user, "owner", "admin", "manager")
    require_permission(conn,user,"cash.manage")
    movement_id, created = new_id(), now_iso()
    amount = decimal_money_cents(data.amount) * (1 if data.kind == "supply" else -1)
    with transaction(conn):
        reg = conn.execute("SELECT * FROM cash_registers WHERE id=? AND company_id=? AND status='open'", (register_id, user["company_id"])).fetchone()
        if not reg:
            raise HTTPException(404, "Caixa aberto não encontrado nesta empresa.")
        movements = conn.execute("SELECT amount_cents FROM cash_movements WHERE register_id=?", (register_id,)).fetchall()
        expected = reg["opening_cents"] + sum(m["amount_cents"] for m in movements)
        if expected + amount < 0:
            raise HTTPException(409, "O caixa não tem saldo esperado suficiente para essa retirada.")
        conn.execute("INSERT INTO cash_movements(id,company_id,register_id,actor_id,kind,amount_cents,reason,sale_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (movement_id, user["company_id"], register_id, user["id"], data.kind, amount, data.reason.strip(), None, created))
        audit(conn, user["company_id"], user["id"], "register.movement", "register", register_id,
              {"kind": data.kind, "amount": cents_to_money(abs(amount)), "reason": data.reason.strip()})
    return {"movement_id": movement_id, "kind": data.kind, "amount": cents_to_money(abs(amount))}


@app.post("/v1/registers/{register_id}/close")
def close_register(register_id: str, data: RegisterCloseIn, conn: DB, user: USER):
    require_role(user, "owner", "admin", "manager")
    require_permission(conn,user,"cash.manage")
    with transaction(conn):
        reg = conn.execute("SELECT * FROM cash_registers WHERE id=? AND company_id=? AND status='open'", (register_id, user["company_id"])).fetchone()
        if not reg:
            raise HTTPException(404, "Caixa aberto não encontrado nesta empresa.")
        movements = conn.execute("SELECT amount_cents FROM cash_movements WHERE register_id=?", (register_id,)).fetchall()
        expected = reg["opening_cents"] + sum(m["amount_cents"] for m in movements)
        counted = decimal_money_cents(data.counted_amount)
        difference = counted - expected
        closed_at = now_iso()
        conn.execute("UPDATE cash_registers SET status='closed',closed_by=?,closed_at=?,counted_cents=?,expected_cents=?,difference_cents=? WHERE id=? AND company_id=?",
                     (user["id"], closed_at, counted, expected, difference, register_id, user["company_id"]))
        audit(conn, user["company_id"], user["id"], "register.closed", "register", register_id,
              {"expected": cents_to_money(expected), "counted": cents_to_money(counted), "difference": cents_to_money(difference)})
    row = conn.execute("SELECT * FROM cash_registers WHERE id=? AND company_id=?", (register_id, user["company_id"])).fetchone()
    return register_json(row)


@app.post("/v1/sales", status_code=201)
def create_sale(data: SaleIn, request: Request, conn: DB, user: USER,
                idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
                response: Response = None):
    require_role(user, "owner", "admin", "manager", "operator")
    require_permission(conn,user,"sales.create")
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(400, "Envie um Idempotency-Key único para evitar vendas duplicadas.")
    if any(line.discount > 0 for line in data.items) and user["role"] not in {"owner", "admin", "manager"}:
        raise HTTPException(403, "Descontos nesta versão exigem gerente ou administrador.")
    normalized = {"items": sorted([{"product_id": x.product_id, "quantity": str(x.quantity), "discount": str(x.discount)} for x in data.items], key=lambda x: x["product_id"]),
                  "payment_method": data.payment_method, "received_amount": str(data.received_amount), "customer_id":data.customer_id}
    request_hash = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    sale_id, created = new_id(), now_iso()
    with transaction(conn):
        prior = conn.execute("SELECT * FROM sales WHERE company_id=? AND idempotency_key=?", (user["company_id"], idempotency_key)).fetchone()
        if prior:
            if prior["request_hash"] != request_hash:
                raise HTTPException(409, "Essa chave de idempotência já foi usada com outros dados.")
            if response is not None:
                response.status_code = 200
            return sale_json(conn, prior)
        if data.customer_id:
            customer=conn.execute("SELECT id FROM customers WHERE id=? AND company_id=? AND active=1",(data.customer_id,user["company_id"])).fetchone()
            if not customer: raise HTTPException(404,"Cliente não encontrado nesta empresa.")
        register = conn.execute("SELECT * FROM cash_registers WHERE company_id=? AND status='open' ORDER BY opened_at DESC LIMIT 1", (user["company_id"],)).fetchone()
        if not register:
            raise HTTPException(409, "Abra um caixa antes de finalizar uma venda.")
        aggregates: dict[str, dict] = {}
        subtotal = 0
        for line in data.items:
            product = company_product(conn, user["company_id"], line.product_id)
            quantity = decimal_milli(line.quantity)
            if quantity <= 0:
                raise HTTPException(422, "A quantidade deve ser positiva.")
            gross = int((Decimal(product["price_cents"]) * Decimal(quantity) / Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            discount = decimal_money_cents(line.discount)
            if discount > gross:
                raise HTTPException(422, "O desconto não pode superar o valor do item.")
            line_total = gross - discount
            if product["stock_milli"] < quantity:
                raise HTTPException(409, "Estoque insuficiente para o produto: " + product["name"])
            if product["id"] not in aggregates:
                aggregates[product["id"]] = {"product": product, "quantity": 0}
            aggregates[product["id"]]["quantity"] += quantity
            if aggregates[product["id"]]["quantity"] > product["stock_milli"]:
                raise HTTPException(409, "Estoque insuficiente para o produto: " + product["name"])
            subtotal += line_total
        received = decimal_money_cents(data.received_amount)
        if received < subtotal:
            raise HTTPException(422, "O valor recebido em dinheiro não cobre o total da venda.")
        change = received - subtotal
        conn.execute("""INSERT INTO sales(id,company_id,register_id,created_by,idempotency_key,request_hash,status,
            subtotal_cents,total_cents,payment_method,received_cents,change_cents,created_at,customer_id)
            VALUES(?,?,?,?,?,?,'paid',?,?,'cash',?,?,?,?)""",
            (sale_id, user["company_id"], register["id"], user["id"], idempotency_key, request_hash,
             subtotal, subtotal, received, change, created, data.customer_id))
        for line in data.items:
            product = aggregates[line.product_id]["product"]
            quantity = decimal_milli(line.quantity)
            gross = int((Decimal(product["price_cents"]) * Decimal(quantity) / Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            discount = decimal_money_cents(line.discount)
            conn.execute("INSERT INTO sale_items(id,sale_id,product_id,sku,name,quantity_milli,unit_price_cents,discount_cents,line_total_cents) VALUES(?,?,?,?,?,?,?,?,?)",
                         (new_id(), sale_id, product["id"], product["sku"], product["name"], quantity,
                          product["price_cents"], discount, gross - discount))
        for product_id, aggregate in aggregates.items():
            product, quantity = aggregate["product"], aggregate["quantity"]
            before, after = product["stock_milli"], product["stock_milli"] - quantity
            conn.execute("UPDATE products SET stock_milli=? WHERE id=? AND company_id=?", (after, product_id, user["company_id"]))
            conn.execute("INSERT INTO stock_movements(id,company_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,sale_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (new_id(), user["company_id"], product_id, user["id"], "sale", -quantity,
                          before, after, "Venda " + sale_id, sale_id, created))
        conn.execute("INSERT INTO cash_movements(id,company_id,register_id,actor_id,kind,amount_cents,reason,sale_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (new_id(), user["company_id"], register["id"], user["id"], "sale", subtotal, "Venda " + sale_id, sale_id, created))
        audit(conn, user["company_id"], user["id"], "sale.completed", "sale", sale_id,
              {"total": cents_to_money(subtotal), "payment_method": "cash", "register_id": register["id"], "item_count": len(data.items)})
        result = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        output = sale_json(conn, result)
    return output


@app.post("/v1/sales/{sale_id}/void")
def void_sale(sale_id: str,data: SaleVoidIn,conn: DB,user: USER):
    require_role(user,"owner","admin","manager")
    require_permission(conn,user,"sales.void")
    created=now_iso()
    with transaction(conn):
        sale=conn.execute("SELECT * FROM sales WHERE id=? AND company_id=?",(sale_id,user["company_id"])).fetchone()
        if not sale: raise HTTPException(404,"Venda não encontrada nesta empresa.")
        if sale["status"]!="paid": raise HTTPException(409,"Somente venda paga pode ser cancelada.")
        register=conn.execute("SELECT * FROM cash_registers WHERE company_id=? AND status='open' ORDER BY opened_at DESC LIMIT 1",(user["company_id"],)).fetchone()
        if not register: raise HTTPException(409,"Abra um caixa para registrar o estorno em dinheiro.")
        current_movements=conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM cash_movements WHERE register_id=?",(register["id"],)).fetchone()[0]
        expected_cash=register["opening_cents"]+current_movements
        if expected_cash<sale["total_cents"]: raise HTTPException(409,"O caixa aberto não tem saldo esperado suficiente para registrar a devolução em dinheiro.")
        items=conn.execute("SELECT * FROM sale_items WHERE sale_id=?",(sale_id,)).fetchall()
        for item in items:
            product=conn.execute("SELECT stock_milli FROM products WHERE id=? AND company_id=?",(item["product_id"],user["company_id"])).fetchone()
            if not product: raise HTTPException(409,"Produto da venda não existe mais; estorno de estoque interrompido.")
            before=product["stock_milli"];after=before+item["quantity_milli"]
            conn.execute("UPDATE products SET stock_milli=? WHERE id=? AND company_id=?",(after,item["product_id"],user["company_id"]))
            conn.execute("INSERT INTO stock_movements(id,company_id,product_id,actor_id,kind,quantity_milli,before_milli,after_milli,reason,sale_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(new_id(),user["company_id"],item["product_id"],user["id"],"reversal",item["quantity_milli"],before,after,"Cancelamento venda "+sale_id+": "+data.reason,sale_id,created))
        conn.execute("UPDATE sales SET status='voided' WHERE id=? AND company_id=?",(sale_id,user["company_id"]))
        conn.execute("INSERT INTO cash_movements(id,company_id,register_id,actor_id,kind,amount_cents,reason,sale_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(new_id(),user["company_id"],register["id"],user["id"],"withdrawal",-sale["total_cents"],"Estorno venda "+sale_id+": "+data.reason,sale_id,created))
        audit(conn,user["company_id"],user["id"],"sale.voided","sale",sale_id,{"reason":data.reason,"amount":cents_to_money(sale["total_cents"]),"stock_items":len(items),"refund_register_id":register["id"],"refund_method":"cash_manual"})
    return {"id":sale_id,"status":"voided","reason":data.reason,"voided_at":created,"refund_register_id":register["id"],"note":"Estorno registrado no caixa aberto; a entrega do dinheiro ao cliente é manual e não confirmada por provedor."}


@app.get("/v1/sales")
def list_sales(conn: DB, user: USER, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)):
    require_role(user,"owner","admin","manager","operator")
    rows = conn.execute("SELECT * FROM sales WHERE company_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                         (user["company_id"], limit, offset)).fetchall()
    return {"items": [sale_json(conn, row) for row in rows], "limit": limit, "offset": offset}


@app.get("/v1/audit-events")
def list_audit_events(conn: DB, user: USER, limit: int = Query(default=100, ge=1, le=200)):
    require_role(user, "owner", "admin", "manager")
    rows = conn.execute("""SELECT a.*,u.full_name AS actor_name FROM audit_events a
        LEFT JOIN users u ON u.id=a.actor_id WHERE a.company_id=? ORDER BY a.created_at DESC LIMIT ?""",
        (user["company_id"], limit)).fetchall()
    return {"items": [{"id": r["id"], "actor_id": r["actor_id"], "actor": r["actor_name"],
                       "action": r["action"], "entity_type": r["entity_type"], "entity_id": r["entity_id"],
                       "details": json.loads(r["details_json"]), "created_at": r["created_at"]} for r in rows]}


@app.get("/v1/reports/sales")
def sales_report(conn: DB,user: USER,start_date: date,end_date: date,group_by: str="day"):
    require_role(user,"owner","admin","manager")
    if end_date < start_date: raise HTTPException(422,"A data final precisa ser igual ou posterior à inicial.")
    if (end_date-start_date).days>366: raise HTTPException(422,"O período máximo deste relatório é 367 dias.")
    if group_by not in {"day","month"}: raise HTTPException(422,"Agrupamento deve ser day ou month.")
    expression="date(created_at)" if group_by=="day" else "strftime('%Y-%m',created_at)"
    rows=conn.execute(f"""SELECT {expression} AS period,COUNT(*) AS sale_count,
        SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) AS paid_count,
        SUM(CASE WHEN status='voided' THEN 1 ELSE 0 END) AS voided_count,
        COALESCE(SUM(CASE WHEN status='paid' THEN total_cents ELSE 0 END),0) AS gross_cents,
        COALESCE(SUM(CASE WHEN status='voided' THEN total_cents ELSE 0 END),0) AS voided_cents
        FROM sales WHERE company_id=? AND date(created_at)>=? AND date(created_at)<=? GROUP BY period ORDER BY period""",
        (user["company_id"],start_date.isoformat(),end_date.isoformat())).fetchall()
    return {"start_date":start_date.isoformat(),"end_date":end_date.isoformat(),"group_by":group_by,
        "items":[{"period":r["period"],"sale_count":r["sale_count"],"paid_count":r["paid_count"],"voided_count":r["voided_count"],"gross_paid":cents_to_money(r["gross_cents"]),"voided_value":cents_to_money(r["voided_cents"])} for r in rows],
        "totals":{"sale_count":sum(r["sale_count"] for r in rows),"paid_count":sum(r["paid_count"] for r in rows),"voided_count":sum(r["voided_count"] for r in rows),"gross_paid":cents_to_money(sum(r["gross_cents"] for r in rows)),"voided_value":cents_to_money(sum(r["voided_cents"] for r in rows))},
        "note":"Relatório de vendas registradas localmente, somente dinheiro nesta versão; datas UTC. Não é demonstrativo fiscal/contábil."}


@app.get("/v1/reports/inventory")
def inventory_report(conn: DB,user: USER,search: str=Query(default="",max_length=100),limit: int=Query(default=200,ge=1,le=500),offset: int=Query(default=0,ge=0)):
    require_role(user,"owner","admin","manager")
    term="%"+search.strip().replace("%","\\%").replace("_","\\_")+"%"
    rows=conn.execute("""SELECT * FROM products WHERE company_id=? AND active=1 AND (?='' OR name LIKE ? ESCAPE '\\' OR sku LIKE ? ESCAPE '\\')
        ORDER BY name LIMIT ? OFFSET ?""",(user["company_id"],search.strip(),term,term,limit,offset)).fetchall()
    total=conn.execute("SELECT COUNT(*) FROM products WHERE company_id=? AND active=1",(user["company_id"],)).fetchone()[0]
    low=conn.execute("SELECT COUNT(*) FROM products WHERE company_id=? AND active=1 AND min_stock_milli>0 AND stock_milli<=min_stock_milli",(user["company_id"],)).fetchone()[0]
    # Keep monetary valuation in arbitrary-precision integer arithmetic. SQLite
    # SUM(stock_milli * cents) can overflow signed 64-bit for valid inputs.
    valuation_rows=conn.execute("SELECT stock_milli,cost_cents,price_cents FROM products WHERE company_id=? AND active=1",(user["company_id"],)).fetchall()
    total_stock_milli=sum(r["stock_milli"] for r in valuation_rows)
    estimated_cost_cents=sum((r["stock_milli"]*r["cost_cents"]+500)//1000 for r in valuation_rows)
    estimated_retail_cents=sum((r["stock_milli"]*r["price_cents"]+500)//1000 for r in valuation_rows)
    items=[]
    for r in rows:
        cost_total=(r["stock_milli"]*r["cost_cents"]+500)//1000
        retail_total=(r["stock_milli"]*r["price_cents"]+500)//1000
        items.append({"product_id":r["id"],"sku":r["sku"],"name":r["name"],"category":r["category"],"unit":r["unit"],"stock":milli_to_decimal(r["stock_milli"]),"minimum_stock":milli_to_decimal(r["min_stock_milli"]),"low_stock":r["min_stock_milli"]>0 and r["stock_milli"]<=r["min_stock_milli"],"unit_cost":cents_to_money(r["cost_cents"]),"unit_price":cents_to_money(r["price_cents"]),"cost_value":cents_to_money(cost_total),"retail_value":cents_to_money(retail_total)})
    return {"items":items,"total":total,"limit":limit,"offset":offset,"summary":{"product_count":total,"low_stock_products":low,"stock_units":milli_to_decimal(total_stock_milli),"estimated_cost_value":cents_to_money(estimated_cost_cents),"estimated_retail_value":cents_to_money(estimated_retail_cents)},"note":"Avaliação estimada pelo custo e preço atuais do cadastro, sem método contábil de custeio, impostos ou movimentação em trânsito."}


@app.get("/v1/dashboard/summary")
def dashboard_summary(conn: DB, user: USER):
    require_role(user,"owner","admin","manager","operator")
    company_id = user["company_id"]
    sales = conn.execute("SELECT COUNT(*) AS count,COALESCE(SUM(total_cents),0) AS total FROM sales WHERE company_id=? AND status='paid' AND date(created_at)=date('now')", (company_id,)).fetchone()
    product_count = conn.execute("SELECT count(*) FROM products WHERE company_id=? AND active=1", (company_id,)).fetchone()[0]
    low_stock = conn.execute("SELECT count(*) FROM products WHERE company_id=? AND active=1 AND min_stock_milli > 0 AND stock_milli <= min_stock_milli", (company_id,)).fetchone()[0]
    reg = conn.execute("SELECT * FROM cash_registers WHERE company_id=? AND status='open' ORDER BY opened_at DESC LIMIT 1", (company_id,)).fetchone()
    return {"date_utc": datetime.now(timezone.utc).date().isoformat(), "sales_today": sales["count"],
            "revenue_today": cents_to_money(sales["total"]), "active_products": product_count,
            "low_stock_products": low_stock, "open_register": register_json(reg) if reg else None,
            "financial_period": "parcial_do_dia_utc", "payments_integrated": False, "fiscal_integrated": False}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")), reload=False)
