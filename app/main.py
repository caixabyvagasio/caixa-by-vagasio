from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
import threading
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
    ("tables_tabs", "Mesas e comandas", "Organização de atendimento; fluxo operacional ainda não incluído", "restaurant", ["restaurant"], ["sales"]),
    ("delivery", "Delivery e retirada", "Opção de atendimento; integração externa não configurada", "restaurant", ["restaurant"], ["sales"]),
    ("kitchen", "Pedidos de preparo", "Preparação e cozinha; integração com impressoras pendente", "restaurant", ["restaurant"], ["sales"]),
    ("variants", "Tamanhos e cores", "Variações de produto; cadastro especializado ainda não incluído", "catalog", ["apparel"], ["inventory"]),
    ("returns", "Trocas e devoluções", "Regras de troca ainda dependem de implementação", "sales", ["apparel","retail"], ["sales"]),
    ("commissions", "Comissões de vendedores", "Apuração ainda depende do módulo de vendas", "sales", ["apparel","services"], ["sales"]),
    ("appointments", "Agendamentos", "Agenda especializada ainda não incluída", "services", ["services"], []),
    ("measurement_sales", "Vendas por medida", "Unidades comerciais especializadas ainda dependem de regras", "catalog", ["construction"], ["inventory"]),
    ("purchasing", "Compras e fornecedores", "Módulo ainda não implementado", "management", ["*"], ["inventory"]),
]

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


def get_db_path() -> str:
    path = Path(DB_PATH)
    if DB_PATH != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    conn = connect_db()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
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
            created_at TEXT NOT NULL, UNIQUE(company_id, idempotency_key)
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
        """)
        existing_columns = {r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()}
        for column, sql in (("category", "ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT ''"),
                            ("barcode", "ALTER TABLE products ADD COLUMN barcode TEXT"),
                            ("min_stock_milli", "ALTER TABLE products ADD COLUMN min_stock_milli INTEGER NOT NULL DEFAULT 0")):
            if column not in existing_columns:
                conn.execute(sql)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS products_company_barcode ON products(company_id, barcode) WHERE barcode IS NOT NULL AND barcode <> ''")
        for segment_id, segment in SEGMENT_CATALOG.items():
            conn.execute("INSERT OR REPLACE INTO segment_catalog(id,name,questions_json,active) VALUES(?,?,?,1)",
                         (segment_id, segment["name"], json.dumps(segment["questions"], ensure_ascii=False)))
        for feature_id, name, description, category, recommended, dependencies in FEATURES:
            conn.execute("INSERT OR REPLACE INTO feature_catalog(id,name,description,category,recommended_segments_json,dependencies_json,available) VALUES(?,?,?,?,?,?,1)",
                         (feature_id, name, description, category, json.dumps(recommended), json.dumps(dependencies)))
    finally:
        conn.close()


def audit(conn: sqlite3.Connection, company_id: str, actor_id: str | None,
          action: str, entity_type: str, entity_id: str, details: dict) -> None:
    conn.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?)",
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
    return str((Decimal(milli) / Decimal(1000)).normalize())


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BootstrapIn(StrictModel):
    company_name: str = Field(min_length=2, max_length=120)
    company_slug: str = Field(min_length=2, max_length=50)
    owner_name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=200)

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


class SaleLineIn(StrictModel):
    product_id: str
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    discount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)


class SaleIn(StrictModel):
    items: list[SaleLineIn] = Field(min_length=1, max_length=100)
    payment_method: Literal["cash"]
    received_amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


bearer = HTTPBearer(auto_error=False)
@asynccontextmanager
async def lifespan(_app):
    for name, secret in (("CAIXA_TOKEN_SECRET", TOKEN_SECRET), ("CAIXA_BOOTSTRAP_SECRET", BOOTSTRAP_SECRET)):
        if not secret:
            raise RuntimeError(f"{name} precisa estar configurado no ambiente, nunca no código-fonte.")
        if len(secret.encode()) < 32:
            raise RuntimeError(f"{name} deve conter no mínimo 32 bytes. Use um segredo aleatório forte.")
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


def require_role(user: dict, *roles: str) -> None:
    if user["role"] not in roles:
        raise HTTPException(403, "Seu perfil não permite esta operação.")


def company_product(conn: sqlite3.Connection, company_id: str, product_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM products WHERE id=? AND company_id=? AND active=1", (product_id, company_id)).fetchone()
    if not row:
        raise HTTPException(404, "Produto não encontrado.")
    return row


def product_json(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "sku": row["sku"], "name": row["name"], "unit": row["unit"],
            "price": cents_to_money(row["price_cents"]), "cost": cents_to_money(row["cost_cents"]),
            "stock": milli_to_decimal(row["stock_milli"]), "category": row["category"], "barcode": row["barcode"],
            "minimum_stock": milli_to_decimal(row["min_stock_milli"]), "low_stock": row["stock_milli"] <= row["min_stock_milli"],
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


@app.get("/", include_in_schema=False)
def frontend_index():
    return FileResponse(_FRONTEND / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "service": "caixa-api", "version": APP_VERSION,
            "mode": "development-foundation", "integrations": {"payments": False, "fiscal": False}}


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


@app.post("/v1/companies/register", status_code=201)
def register_company(data: BootstrapIn, request: Request, conn: DB):
    """Self-service tenant creation, disabled until the operator explicitly enables it."""
    if not PUBLIC_SIGNUP:
        raise HTTPException(404, "Cadastro público temporariamente indisponível.")
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(f"signup:{client_ip}", 3, 3600)
    company_id, user_id, created = new_id(), new_id(), now_iso()
    try:
        with transaction(conn):
            conn.execute("INSERT INTO companies VALUES(?,?,?,'active',?)",
                         (company_id, data.company_name.strip(), data.company_slug, created))
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,'owner',1,?)",
                         (user_id, company_id, data.email, data.owner_name.strip(), password_hash(data.password), created))
            audit(conn, company_id, user_id, "company.self_registered", "company", company_id,
                  {"company_slug": data.company_slug, "actor_email": data.email})
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
def configuration_catalog(conn: DB, user: USER):
    segments = conn.execute("SELECT * FROM segment_catalog WHERE active=1 ORDER BY name").fetchall()
    features = conn.execute("SELECT * FROM feature_catalog WHERE available=1 ORDER BY category,name").fetchall()
    profile = conn.execute("SELECT * FROM company_profiles WHERE company_id=?", (user["company_id"],)).fetchone()
    return {"segments": [{"id":r["id"],"name":r["name"],"questions":json.loads(r["questions_json"])} for r in segments],
            "features": [{"id":r["id"],"name":r["name"],"description":r["description"],"category":r["category"],
                          "recommended_segments":json.loads(r["recommended_segments_json"]),"dependencies":json.loads(r["dependencies_json"]),
                          "available":bool(r["available"])} for r in features],
            "profile": ({"primary_segment":profile["primary_segment"],
                         "secondary_segments":json.loads(profile["secondary_segments_json"]),
                         "answers":json.loads(profile["answers_json"]),"modules":json.loads(profile["modules_json"]),
                         "updated_at":profile["updated_at"]} if profile else None),
            "plan_entitlements": "not_configured", "activation_note": "Módulos sugeridos não significam contratação nem ativação de recursos ainda não implementados."}


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
    modules = {"sales", "inventory"}
    if data.answers.get("barcode"): modules.add("barcode")
    if data.answers.get("weighed_items"): modules.add("weighed_items")
    if data.answers.get("perishables"): modules.add("expiry_control")
    if any(data.answers.get(k,False) for k in ("tables","tabs")): modules.add("tables_tabs")
    if data.answers.get("delivery"): modules.add("delivery")
    if data.answers.get("kitchen"): modules.add("kitchen")
    if data.answers.get("sizes_colors"): modules.add("variants")
    if data.answers.get("returns"): modules.add("returns")
    if data.answers.get("commission"): modules.add("commissions")
    if data.answers.get("appointments"): modules.add("appointments")
    if data.answers.get("measurement"): modules.add("measurement_sales")
    modules = sorted(modules)
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
            audit(conn, user["company_id"], user["id"], "user.created", "user", user_id, {"email": data.email, "role": data.role})
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Já existe um usuário com esse e-mail nesta empresa.")
    return {"id": user_id, "company_id": user["company_id"], "email": data.email, "name": data.full_name.strip(), "role": data.role, "active": True}


@app.post("/v1/products", status_code=201)
def create_product(data: ProductIn, conn: DB, user: USER):
    require_role(user, "owner", "admin", "manager", "stock")
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


@app.patch("/v1/users/{user_id}")
def update_user(user_id: str, data: UserStatusIn, conn: DB, user: USER):
    require_role(user, "owner", "admin")
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
    return {"items": [product_json(row) for row in rows], "total": count, "limit": limit, "offset": offset}


@app.get("/v1/products/low-stock")
def low_stock_products(conn: DB, user: USER, limit: int = Query(default=100, ge=1, le=200)):
    rows=conn.execute("SELECT * FROM products WHERE company_id=? AND active=1 AND min_stock_milli>0 AND stock_milli<=min_stock_milli ORDER BY (min_stock_milli-stock_milli) DESC LIMIT ?",
                      (user["company_id"],limit)).fetchall()
    return {"items":[product_json(row) for row in rows],"total":len(rows)}


@app.post("/v1/products/{product_id}/stock-adjustments", status_code=201)
def adjust_stock(product_id: str, data: StockAdjustmentIn, conn: DB, user: USER):
    require_role(user, "owner", "admin", "manager", "stock")
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
        conn.execute("INSERT INTO stock_movements VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (movement_id, user["company_id"], product_id, user["id"], kind, signed_quantity,
                      before, after, data.reason, None, created))
        audit(conn, user["company_id"], user["id"], "stock.adjusted", "product", product_id,
              {"direction": data.direction, "quantity": milli_to_decimal(quantity), "before": milli_to_decimal(before),
               "after": milli_to_decimal(after), "reason": data.reason})
    return {"movement_id": movement_id, "product_id": product_id, "before": milli_to_decimal(before),
            "after": milli_to_decimal(after), "reason": data.reason}


@app.get("/v1/stock-movements")
def list_stock_movements(conn: DB, user: USER, limit: int = Query(default=100, ge=1, le=200)):
    rows = conn.execute("""SELECT m.*,p.sku,p.name,u.full_name AS actor_name FROM stock_movements m
        JOIN products p ON p.id=m.product_id JOIN users u ON u.id=m.actor_id
        WHERE m.company_id=? ORDER BY m.created_at DESC LIMIT ?""", (user["company_id"], limit)).fetchall()
    return {"items": [{"id": r["id"], "product_id": r["product_id"], "sku": r["sku"], "product": r["name"],
                       "actor": r["actor_name"], "kind": r["kind"], "quantity": milli_to_decimal(abs(r["quantity_milli"])),
                       "before": milli_to_decimal(r["before_milli"]), "after": milli_to_decimal(r["after_milli"]),
                       "reason": r["reason"], "sale_id": r["sale_id"], "created_at": r["created_at"]} for r in rows]}


@app.post("/v1/registers/open", status_code=201)
def open_register(data: RegisterOpenIn, conn: DB, user: USER):
    require_role(user, "owner", "admin", "manager", "operator")
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
    row = conn.execute("SELECT * FROM cash_registers WHERE company_id=? AND status='open' ORDER BY opened_at DESC LIMIT 1", (user["company_id"],)).fetchone()
    if not row:
        return {"register": None}
    movements = conn.execute("SELECT kind,amount_cents FROM cash_movements WHERE register_id=?", (row["id"],)).fetchall()
    expected = row["opening_cents"] + sum(m["amount_cents"] for m in movements)
    return {"register": register_json(row), "expected_amount": cents_to_money(expected)}


@app.post("/v1/registers/{register_id}/movements", status_code=201)
def cash_movement(register_id: str, data: CashMovementIn, conn: DB, user: USER):
    require_role(user, "owner", "admin", "manager")
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
        conn.execute("INSERT INTO cash_movements VALUES(?,?,?,?,?,?,?,?,?)",
                     (movement_id, user["company_id"], register_id, user["id"], data.kind, amount, data.reason.strip(), None, created))
        audit(conn, user["company_id"], user["id"], "register.movement", "register", register_id,
              {"kind": data.kind, "amount": cents_to_money(abs(amount)), "reason": data.reason.strip()})
    return {"movement_id": movement_id, "kind": data.kind, "amount": cents_to_money(abs(amount))}


@app.post("/v1/registers/{register_id}/close")
def close_register(register_id: str, data: RegisterCloseIn, conn: DB, user: USER):
    require_role(user, "owner", "admin", "manager")
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
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(400, "Envie um Idempotency-Key único para evitar vendas duplicadas.")
    if any(line.discount > 0 for line in data.items) and user["role"] not in {"owner", "admin", "manager"}:
        raise HTTPException(403, "Descontos nesta versão exigem gerente ou administrador.")
    normalized = {"items": sorted([{"product_id": x.product_id, "quantity": str(x.quantity), "discount": str(x.discount)} for x in data.items], key=lambda x: x["product_id"]),
                  "payment_method": data.payment_method, "received_amount": str(data.received_amount)}
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
            subtotal_cents,total_cents,payment_method,received_cents,change_cents,created_at)
            VALUES(?,?,?,?,?,?,'paid',?,?,'cash',?,?,?)""",
            (sale_id, user["company_id"], register["id"], user["id"], idempotency_key, request_hash,
             subtotal, subtotal, received, change, created))
        for line in data.items:
            product = aggregates[line.product_id]["product"]
            quantity = decimal_milli(line.quantity)
            gross = int((Decimal(product["price_cents"]) * Decimal(quantity) / Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            discount = decimal_money_cents(line.discount)
            conn.execute("INSERT INTO sale_items VALUES(?,?,?,?,?,?,?,?,?)",
                         (new_id(), sale_id, product["id"], product["sku"], product["name"], quantity,
                          product["price_cents"], discount, gross - discount))
        for product_id, aggregate in aggregates.items():
            product, quantity = aggregate["product"], aggregate["quantity"]
            before, after = product["stock_milli"], product["stock_milli"] - quantity
            conn.execute("UPDATE products SET stock_milli=? WHERE id=? AND company_id=?", (after, product_id, user["company_id"]))
            conn.execute("INSERT INTO stock_movements VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (new_id(), user["company_id"], product_id, user["id"], "sale", -quantity,
                          before, after, "Venda " + sale_id, sale_id, created))
        conn.execute("INSERT INTO cash_movements VALUES(?,?,?,?,?,?,?,?,?)",
                     (new_id(), user["company_id"], register["id"], user["id"], "sale", subtotal, "Venda " + sale_id, sale_id, created))
        audit(conn, user["company_id"], user["id"], "sale.completed", "sale", sale_id,
              {"total": cents_to_money(subtotal), "payment_method": "cash", "register_id": register["id"], "item_count": len(data.items)})
        result = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        output = sale_json(conn, result)
    return output


@app.get("/v1/sales")
def list_sales(conn: DB, user: USER, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)):
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


@app.get("/v1/dashboard/summary")
def dashboard_summary(conn: DB, user: USER):
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
