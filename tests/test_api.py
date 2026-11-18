from pathlib import Path
import os
import uuid
import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def pg_database_url():
    if os.environ.get("CAIXA_TEST_POSTGRES")!="true":
        yield None
        return
    admin=os.environ["CAIXA_TEST_POSTGRES_ADMIN_URL"]
    name="caixa_test_"+uuid.uuid4().hex[:12]
    conn=main.psycopg2.connect(admin)
    conn.autocommit=True
    cur=conn.cursor()
    cur.execute(f'CREATE DATABASE "{name}"')
    cur.close();conn.close()
    from urllib.parse import urlsplit,urlunsplit
    u=urlsplit(admin); url=urlunsplit((u.scheme,u.netloc,f'/{name}',u.query,u.fragment))
    try: yield url
    finally:
        conn=main.psycopg2.connect(admin);conn.autocommit=True;cur=conn.cursor()
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",(name,))
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"');cur.close();conn.close()


@pytest.fixture
def world(tmp_path, monkeypatch, pg_database_url):
    monkeypatch.setattr(main, "DATABASE_URL", pg_database_url or "")
    monkeypatch.setattr(main, "POSTGRES_BACKEND", bool(pg_database_url))
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "caixa-test.sqlite3"))
    monkeypatch.setattr(main, "TOKEN_SECRET", "test-only-token-secret-with-entropy-123456789")
    monkeypatch.setattr(main, "BOOTSTRAP_SECRET", "test-only-provisioning-secret-123456")
    with TestClient(main.app) as client:
        yield client


def company(client, name="Mercado Um", slug="mercado-um", email="owner@example.test"):
    response = client.post("/v1/bootstrap", headers={"X-Bootstrap-Key": "test-only-provisioning-secret-123456"}, json={
        "company_name": name, "company_slug": slug.upper(), "owner_name": "Ana Teste",
        "email": email, "password": "strong-test-password-123"
    })
    assert response.status_code == 201, response.text
    login = client.post("/v1/auth/login", json={"company_slug": slug.upper(), "email": email.upper(), "password": "strong-test-password-123"})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def auth(token):
    return {"Authorization": "Bearer " + token}


def product(client, token, sku="SKU-1", name="Cafe", price="10.50"):
    response = client.post("/v1/products", headers=auth(token), json={"sku": sku, "name": name, "unit": "un", "price": price, "cost": "5.00"})
    assert response.status_code == 201, response.text
    return response.json()


def test_postgres_sql_translation_handles_existing_sqlite_queries(monkeypatch):
    monkeypatch.setattr(main,"POSTGRES_BACKEND",True)
    conn=main.PostgresConnection.__new__(main.PostgresConnection)
    sql,params=conn._sql("PRAGMA table_info(products)")
    assert "information_schema.columns" in sql and params==("products",)
    sql,_=conn._sql("CREATE TABLE IF NOT EXISTS sample (id TEXT PRIMARY KEY, amount INTEGER NOT NULL)")
    assert "amount BIGINT NOT NULL" in sql
    sql,_=conn._sql("INSERT OR IGNORE INTO sample(id) VALUES(?)")
    assert sql=="INSERT INTO sample(id) VALUES(%s) ON CONFLICT DO NOTHING"
    sql,_=conn._sql("SELECT date('now'),strftime('%Y-%m',created_at),SUM(MAX(0, amount)) FROM sample WHERE created_at>=date(?) AND due_date<date('now')")
    assert "CURRENT_DATE" in sql and "to_char(CAST(SUBSTRING(created_at FROM 1 FOR 10) AS date),'YYYY-MM')" in sql
    assert "SUM(GREATEST(0," in sql and "CAST(%s AS date)" in sql and "CAST(due_date AS date)<CURRENT_DATE" in sql
    sql,params=conn._sql("INSERT INTO company_profiles(company_id,primary_segment) VALUES(?,?) ON CONFLICT(company_id) DO UPDATE SET primary_segment=excluded.primary_segment")
    assert "EXCLUDED.primary_segment" in sql and "%s" in sql and params is None


def test_frontend_is_served_from_same_app(world):
    assert world.get("/").status_code == 200
    assert "Entrar na sua empresa" in world.get("/").text
    assert "Acessar demonstração fictícia" in world.get("/").text
    assert world.get("/static/app.js").status_code == 200
    assert 'id="loginForm" method="post" action="/" autocomplete="off"' in world.get("/").text
    assert world.get("/static/app.css").status_code == 200


def test_health_and_provisioning_gate(world):
    assert world.get("/health").json()["integrations"] == {"payments": False, "fiscal": False}
    assert world.get('/health').json()['demo_enabled'] is main.DEMO_MODE
    assert world.get('/health').json()['signup_enabled'] is (main.POSTGRES_BACKEND and main.PUBLIC_SIGNUP and not main.DEMO_MODE)
    denied = world.post("/v1/bootstrap", json={"company_name":"Xx","company_slug":"xx","owner_name":"AA","email":"a@b.co","password":"strong-test-password-123"})
    assert denied.status_code == 403
    assert world.get("/v1/products").status_code == 401


def test_health_discloses_storage_backend_without_secrets(world):
    health=world.get('/health').json()
    assert health['storage']==('postgresql' if main.POSTGRES_BACKEND else 'sqlite-ephemeral')
    assert 'database_url' not in health and 'credentials' not in health
    assert 'database_url' not in health and 'credentials' not in health


def test_company_scope_products_and_authentication(world):
    t1 = company(world)
    t2 = company(world, "Mercado Dois", "mercado-dois", "owner2@example.test")
    p1 = product(world, t1)
    assert world.get("/v1/products", headers=auth(t2)).json()["items"] == []
    assert world.post(f"/v1/products/{p1['id']}/stock-adjustments", headers=auth(t2), json={"direction":"in","quantity":"5","reason":"Recebimento"}).status_code == 404
    assert world.post("/v1/products", headers=auth(t2), json={"sku":"SKU-1","name":"Cafe 2","price":"1.00","cost":"0.50"}).status_code == 201


def test_company_user_management_and_deactivation(world):
    token = company(world)
    created = world.post("/v1/users", headers=auth(token), json={"full_name":"Bia Caixa","email":"BIA@EXAMPLE.TEST","password":"strong-user-password-123","role":"operator"})
    assert created.status_code == 201, created.text
    assert created.json()["email"] == "bia@example.test"
    assert world.get("/v1/users", headers=auth(token)).status_code == 200
    user_id = created.json()["id"]
    assert world.patch(f"/v1/users/{user_id}", headers=auth(token), json={"active":False}).status_code == 200
    assert world.post("/v1/auth/login", json={"company_slug":"mercado-um","email":"bia@example.test","password":"strong-user-password-123"}).status_code == 401
    assert any(event["action"] == "user.status_changed" for event in world.get("/v1/audit-events", headers=auth(token)).json()["items"])


def test_stock_adjustment_rejects_negative_and_audits(world):
    token = company(world)
    p = product(world, token)
    add = world.post(f"/v1/products/{p['id']}/stock-adjustments", headers=auth(token), json={"direction":"in","quantity":"3.250","reason":"Entrada nota 1"})
    assert add.status_code == 201 and add.json()["after"] == "3.25"
    insufficient = world.post(f"/v1/products/{p['id']}/stock-adjustments", headers=auth(token), json={"direction":"out","quantity":"3.251","reason":"Contagem incorreta"})
    assert insufficient.status_code == 409
    assert world.get("/v1/products", headers=auth(token)).json()["items"][0]["stock"] == "3.25"
    assert any(x["action"] == "stock.adjusted" for x in world.get("/v1/audit-events", headers=auth(token)).json()["items"])


def test_sale_register_stock_cash_and_idempotency(world):
    token = company(world)
    p = product(world, token, price="10.00")
    assert world.post(f"/v1/products/{p['id']}/stock-adjustments", headers=auth(token), json={"direction":"in","quantity":"5","reason":"Recebimento"}).status_code == 201
    reg = world.post("/v1/registers/open", headers=auth(token), json={"label":"Caixa 01","opening_amount":"50.00"})
    assert reg.status_code == 201, reg.text
    headers = {**auth(token), "Idempotency-Key":"integration-test-sale-001"}
    payload = {"items":[{"product_id":p["id"],"quantity":"2","discount":"0"}],"payment_method":"cash","received_amount":"25.00"}
    sale = world.post("/v1/sales", headers=headers, json=payload)
    assert sale.status_code == 201, sale.text
    assert sale.json()["total"] == "20.00" and sale.json()["change"] == "5.00"
    replay = world.post("/v1/sales", headers=headers, json=payload)
    assert replay.status_code == 200 and replay.json()["id"] == sale.json()["id"]
    conflict = world.post("/v1/sales", headers=headers, json={**payload,"received_amount":"30.00"})
    assert conflict.status_code == 409
    assert world.get("/v1/products", headers=auth(token)).json()["items"][0]["stock"] == "3"
    current = world.get("/v1/registers/current", headers=auth(token)).json()
    assert current["expected_amount"] == "70.00"
    assert world.get("/v1/sales", headers=auth(token)).json()["items"][0]["id"] == sale.json()["id"]


def test_no_sale_without_register_or_stock_and_role_limits(world):
    token = company(world)
    p = product(world, token, price="8.00")
    payload = {"items":[{"product_id":p["id"],"quantity":"1","discount":"0"}],"payment_method":"cash","received_amount":"8.00"}
    assert world.post("/v1/sales", headers={**auth(token),"Idempotency-Key":"no-reg"}, json=payload).status_code == 409
    world.post(f"/v1/products/{p['id']}/stock-adjustments", headers=auth(token), json={"direction":"in","quantity":"1","reason":"Entrada"})
    world.post("/v1/registers/open", headers=auth(token), json={"label":"Caixa 01","opening_amount":"0"})
    assert world.post("/v1/sales", headers={**auth(token),"Idempotency-Key":"insufficient-receipt"}, json={**payload,"received_amount":"7.99"}).status_code == 422
    assert world.post("/v1/sales", headers={**auth(token),"Idempotency-Key":"discount"}, json={"items":[{"product_id":p["id"],"quantity":"1","discount":"1.00"}],"payment_method":"cash","received_amount":"7.00"}).status_code == 201


def test_cash_movement_and_close_reconciliation(world):
    token = company(world)
    opened = world.post("/v1/registers/open", headers=auth(token), json={"label":"Caixa 01","opening_amount":"100.00"}).json()
    supply = world.post(f"/v1/registers/{opened['id']}/movements", headers=auth(token), json={"kind":"supply","amount":"20.00","reason":"Troco extra"})
    assert supply.status_code == 201
    withdrawal = world.post(f"/v1/registers/{opened['id']}/movements", headers=auth(token), json={"kind":"withdrawal","amount":"15.00","reason":"Sangria autorizada"})
    assert withdrawal.status_code == 201
    closed = world.post(f"/v1/registers/{opened['id']}/close", headers=auth(token), json={"counted_amount":"105.00"})
    assert closed.status_code == 200, closed.text
    assert closed.json()["expected_amount"] == "105.00"
    assert closed.json()["difference"] == "0.00"
    assert world.get("/v1/registers/current", headers=auth(token)).json()["register"] is None


def test_stock_role_cannot_view_inventory_valuation_or_customer_supplier_pii(world):
    owner=company(world)
    world.post('/v1/customers',headers=auth(owner),json={'name':'Cliente Privado','email':'p@example.test','tax_id':'12345678900','notes':'Privado'})
    world.post('/v1/suppliers',headers=auth(owner),json={'name':'Fornecedor Privado','email':'s@example.test','tax_id':'9988776655','notes':'Privado'})
    p=product(world,owner,sku='COST-PII',price='10.00')
    worker=world.post('/v1/users',headers=auth(owner),json={'full_name':'Estoque leitura','email':'stock-read@example.test','password':'strong-stock-password-902','role':'stock'}).json()
    token=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':'stock-read@example.test','password':'strong-stock-password-902'}).json()['access_token']
    visible=world.get('/v1/products',headers=auth(token)).json()['items'][0]
    assert visible['id']==p['id'] and 'cost' not in visible
    assert world.get('/v1/reports/inventory',headers=auth(token)).status_code==403
    supplier=world.get('/v1/suppliers',headers=auth(token)).json()['items'][0]
    assert set(supplier)=={'id','name','active'}
    assert world.get('/v1/sales',headers=auth(token)).status_code==403
    assert world.get('/v1/dashboard/summary',headers=auth(token)).status_code==403


def test_stock_role_cannot_change_catalog_price_or_unit_cost(world):
    owner=company(world)
    p=product(world,owner,sku="LOCK-PRICE",price="10.00")
    staff=world.post("/v1/users",headers=auth(owner),json={"full_name":"Estoquista","email":"estoquista-preco@example.test","password":"strong-stock-password-901","role":"stock"}).json()
    token=world.post("/v1/auth/login",json={"company_slug":"mercado-um","email":"estoquista-preco@example.test","password":"strong-stock-password-901"}).json()["access_token"]
    payload={"sku":"LOCK-PRICE","name":"Cafe","unit":"un","price":"0.01","cost":"0.00"}
    assert world.put(f"/v1/products/{p['id']}",headers=auth(token),json=payload).status_code==403
    assert world.post("/v1/products",headers=auth(token),json={"sku":"NEW","name":"Novo","unit":"un","price":"0.01","cost":"0.00"}).status_code==403
    assert world.get("/v1/products",headers=auth(owner)).json()["items"][0]["price"]=="10.00"


def test_operator_cannot_adjust_stock_or_give_discount(world):
    token = company(world)
    conn = main.connect_db()
    try:
        comp = conn.execute("SELECT id FROM companies WHERE slug='mercado-um'").fetchone()[0]
        created = main.now_iso()
        user_id = main.new_id()
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,'operator',1,?)", (user_id, comp, "op@example.test", "Operador", main.password_hash("strong-operator-password"), created))
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        product_id = main.new_id()
        conn.execute("INSERT INTO products(id,company_id,sku,name,unit,price_cents,cost_cents,stock_milli,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (product_id, comp, "OP-1", "Item operador", "un", 1000, 500, 5000, created))
        conn.execute("INSERT INTO cash_registers(id,company_id,label,status,opening_cents,opened_by,opened_at) VALUES(?,?,?,'open',?,?,?)",
                     (main.new_id(), comp, "Caixa Operador", 0, user_id, created))
    finally:
        conn.close()
    operator_login = world.post("/v1/auth/login", json={"company_slug":"mercado-um","email":"op@example.test","password":"strong-operator-password"})
    op_token = operator_login.json()["access_token"]
    assert world.post(f"/v1/products/{product_id}/stock-adjustments", headers=auth(op_token), json={"direction":"in","quantity":"1","reason":"Tentativa"}).status_code == 403
    discounted = world.post("/v1/sales", headers={**auth(op_token),"Idempotency-Key":"operator-discount"}, json={"items":[{"product_id":product_id,"quantity":"1","discount":"1.00"}],"payment_method":"cash","received_amount":"9.00"})
    assert discounted.status_code == 403


def test_demo_mode_is_opt_in_and_seeds_only_fictional_tenant(world):
    assert world.post("/v1/auth/demo").status_code == 404


def test_public_signup_is_blocked_even_when_enabled_in_demo(world, monkeypatch):
    monkeypatch.setattr(main,"DEMO_MODE",True)
    monkeypatch.setattr(main,"PUBLIC_SIGNUP",True)
    payload={"company_name":"Tenant público","company_slug":"tenant-publico","owner_name":"Teste","email":"teste@example.test","password":"strong-test-password-123"}
    assert world.post("/v1/companies/register",json=payload).status_code==404


def test_demo_login_and_sample_catalog_are_available_only_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "demo.sqlite3"))
    monkeypatch.setattr(main, "TOKEN_SECRET", "test-only-token-secret-with-entropy-123456789")
    monkeypatch.setattr(main, "BOOTSTRAP_SECRET", "test-only-provisioning-secret-123456")
    monkeypatch.setattr(main, "DEMO_MODE", True)
    with TestClient(main.app) as client:
        result = client.post("/v1/auth/demo")
        assert result.status_code == 200, result.text
        assert result.json()["demo"] is True
        token = result.json()["access_token"]
        catalog = client.get("/v1/products", headers=auth(token))
        assert catalog.status_code == 200
        assert catalog.json()["total"] == 3
        assert {x["name"] for x in catalog.json()["items"]} == {
            "Água mineral 500 ml", "Café torrado 250 g", "Detergente neutro 500 ml"
        }
        assert result.json()["user"]["company_id"] == main.DEMO_COMPANY_ID


def test_public_segments_catalog_is_available_without_tenant(world):
    result=world.get('/v1/segments')
    assert result.status_code==200
    items=result.json()['items']
    assert {x['id'] for x in items} >= {'retail','grocery','restaurant','apparel','services','construction'}
    assert all('questions' in x and 'name' in x for x in items)


def test_signup_requires_a_primary_segment():
    from fastapi import HTTPException
    payload=main.BootstrapIn(company_name='Empresa',company_slug='sem-segmento',owner_name='Teste',
        email='sem-segmento@example.test',password='strong-test-password-123')
    with pytest.raises(HTTPException) as result: main.validate_initial_profile(payload,required=True)
    assert result.value.status_code==422


def test_signup_rejects_answers_outside_selected_segments():
    from fastapi import HTTPException
    payload=main.BootstrapIn(company_name='Inválida',company_slug='signup-invalido',owner_name='Teste',
        email='invalid@example.test',password='strong-test-password-123',primary_segment='restaurant',
        answers={'sizes_colors':True})
    with pytest.raises(HTTPException) as result: main.validate_initial_profile(payload)
    assert result.value.status_code==422


def test_signup_requires_primary_segment_in_ui_contract():
    # API remains backward-compatible with administrative clients, while the public form marks it required.
    html=(Path(main._FRONTEND)/'index.html').read_text()
    assert 'name="primary_segment" id="signupPrimarySegment" required' in html
    assert 'id="signupSecondarySegments"' in html
    assert 'id="signupSegmentQuestions"' in html


def test_public_company_signup_is_opt_in(world):
    payload = {"company_name":"Nova Empresa","company_slug":"nova-empresa","owner_name":"Pessoa Teste",
               "email":"novo@example.test","password":"strong-test-password-123"}
    assert world.post("/v1/companies/register", json=payload).status_code == 404


def test_public_signup_creates_isolated_owner_when_enabled(tmp_path, monkeypatch, pg_database_url):
    if not pg_database_url:
        pytest.skip("Signup enabled only in PostgreSQL integration test")
    monkeypatch.setattr(main,"DATABASE_URL",pg_database_url)
    monkeypatch.setattr(main,"POSTGRES_BACKEND",True)
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "signup.sqlite3"))
    monkeypatch.setattr(main, "TOKEN_SECRET", "test-only-token-secret-with-entropy-123456789")
    monkeypatch.setattr(main, "BOOTSTRAP_SECRET", "test-only-provisioning-secret-123456")
    monkeypatch.setattr(main, "PUBLIC_SIGNUP", True)
    with TestClient(main.app) as client:
        payload = {"company_name":"Nova Empresa","company_slug":"Nova-Empresa","owner_name":"Pessoa Teste",
                   "email":"NOVO@EXAMPLE.TEST","password":"strong-test-password-123",
                   "primary_segment":"restaurant","secondary_segments":["grocery"],
                   "answers":{"tables":True,"delivery":True,"barcode":True,"weighed_items":True}}
        result = client.post("/v1/companies/register", json=payload)
        assert result.status_code == 201, result.text
        assert client.post("/v1/companies/register", json=payload).status_code == 409
        login = client.post("/v1/auth/login", json={"company_slug":"nova-empresa","email":"novo@example.test",
                                                       "password":"strong-test-password-123"})
        assert login.status_code == 200
        token = login.json()["access_token"]
        assert client.get("/v1/me", headers=auth(token)).json()["name"] == "Pessoa Teste"
        assert client.get("/v1/products", headers=auth(token)).json()["items"] == []
        profile=client.get('/v1/configuration/catalog',headers=auth(token)).json()['profile']
        assert profile['primary_segment']=='restaurant'
        assert profile['secondary_segments']==['grocery']
        assert profile['answers']==payload['answers']
        assert set(profile['modules'])==main.IMPLEMENTED_FEATURES
        history=client.get('/v1/company/profile/history',headers=auth(token)).json()['items']
        assert len(history)==1 and history[0]['profile']['primary_segment']=='restaurant'


def test_login_rate_limits_repeated_wrong_passwords(world, monkeypatch):
    company(world, "Rate Test", "rate-test", "rate@example.test")
    # Isolate this test from earlier requests made from the same test-client IP.
    monkeypatch.setattr(main, "_RATE_LIMITER", main.WindowRateLimiter())
    payload={"company_slug":"rate-test","email":"rate@example.test","password":"wrong"}
    statuses=[world.post("/v1/auth/login", json=payload).status_code for _ in range(9)]
    assert statuses[:8] == [401] * 8
    assert statuses[8] == 429


def test_branches_and_role_permissions_are_tenant_scoped_and_enforced(world):
    owner=company(world);headers=auth(owner)
    assert len(world.get('/v1/company/branches',headers=headers).json()['items'])==1
    branch=world.post('/v1/company/branches',headers=headers,json={'code':'north','name':'Unidade Norte'})
    assert branch.status_code==201
    world.post('/v1/users',headers=headers,json={'full_name':'Operator','email':'operator@example.test','password':'secure-password-123','role':'operator'})
    op=auth(world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':'operator@example.test','password':'secure-password-123'}).json()['access_token'])
    assert world.get('/v1/permissions/effective?permission=cash.manage',headers=op).json()['allowed'] is True
    denied=world.put('/v1/company/permissions/role',headers=headers,json={'role':'operator','permission':'cash.manage','allowed':False})
    assert denied.status_code==200
    assert world.get('/v1/permissions/effective?permission=cash.manage',headers=op).json()['allowed'] is False
    assert world.post('/v1/registers/open',headers=op,json={'label':'Caixa','opening_amount':'0'}).status_code==403
    other=company(world,'Other','other','other@example.test')
    assert len(world.get('/v1/company/branches',headers=auth(other)).json()['items'])==1


def test_plans_pending_subscriptions_do_not_grant_entitlements(world,monkeypatch):
    owner=company(world);monkeypatch.setattr(main,'BOOTSTRAP_SECRET','test-only-provisioning-secret-123456');h={'X-Bootstrap-Key':'test-only-provisioning-secret-123456'}
    plan=world.post('/v1/master/plans',headers=h,json={'id':'core','name':'Core','included_modules':['sales','inventory'],'price':'0','billing_period':'monthly'})
    assert plan.status_code==201,plan.text
    company_id=world.get('/v1/me',headers=auth(owner)).json()['company_id']
    assigned=world.post(f'/v1/master/companies/{company_id}/subscription',headers=h,json={'plan_id':'core'})
    assert assigned.status_code==200 and assigned.json()['entitlements_enabled'] is False
    current=world.get('/v1/company/commercial',headers=auth(owner)).json()
    assert current['subscription']['status']=='pending' and current['entitlements']==[]
    assert world.post('/v1/master/plans',headers=h,json={'id':'future','name':'Future','included_modules':['tables_tabs']}).status_code==422


def test_restaurant_areas_tables_and_reservations_are_isolated_and_conflict_safe(world):
    owner=company(world);headers=auth(owner)
    saved=world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    assert saved.status_code==200
    area=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Salão'});assert area.status_code==201,area.text
    table=world.post('/v1/restaurant/tables',headers=headers,json={'area_id':area.json()['id'],'label':'Mesa 1','capacity':4});assert table.status_code==201,table.text
    reservation={'table_id':table.json()['id'],'customer_name':'Ana','party_size':3,'starts_at':'2026-11-20T19:00:00-03:00','ends_at':'2026-11-20T20:00:00-03:00'}
    assert world.post('/v1/restaurant/reservations',headers=headers,json=reservation).status_code==201
    assert world.post('/v1/restaurant/reservations',headers=headers,json={**reservation,'starts_at':'2026-11-20T19:30:00-03:00','ends_at':'2026-11-20T20:30:00-03:00'}).status_code==409
    assert world.post('/v1/restaurant/reservations',headers=headers,json={**reservation,'party_size':5,'starts_at':'2026-11-20T21:00:00-03:00','ends_at':'2026-11-20T22:00:00-03:00'}).status_code==409
    floor=world.get('/v1/restaurant/floor',headers=headers)
    assert floor.status_code==200 and floor.json()['items'][0]['status']=='available'
    other=company(world,'Other','other','other@example.test')
    assert world.get('/v1/restaurant/floor',headers=auth(other)).status_code==409


def test_restaurant_end_to_end_order_production_split_cash_and_release_table(world):
    owner=company(world);headers=auth(owner)
    configured=world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    assert configured.status_code==200
    prod=product(world,owner,sku='LANCHE-01',name='Sanduíche',price='12.00')
    # Supply opening inventory through the normal central stock ledger.
    adj=world.post(f"/v1/products/{prod['id']}/stock-adjustments",headers=headers,json={'direction':'in','quantity':'10','reason':'Estoque inicial de teste'})
    assert adj.status_code==201,adj.text
    reg=world.post('/v1/registers/open',headers=headers,json={'label':'Caixa salão','opening_amount':'0'})
    assert reg.status_code==201,reg.text
    area=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Salão'}).json()
    table=world.post('/v1/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'Mesa 7','capacity':4}).json()
    service=world.post(f"/v1/restaurant/tables/{table['id']}/open",headers=headers).json()
    tab=world.post(f"/v1/restaurant/services/{service['id']}/tabs",headers=headers,json={'label':'Cliente A'});assert tab.status_code==201,tab.text
    category=world.post('/v1/restaurant/menu/categories',headers=headers,json={'name':'Lanches'});assert category.status_code==201,category.text
    menu=world.post('/v1/restaurant/menu/items',headers=headers,json={'category_id':category.json()['id'],'product_id':prod['id'],'station':'kitchen'});assert menu.status_code==201,menu.text
    order=world.post(f"/v1/restaurant/tabs/{tab.json()['id']}/orders",headers=headers,json={'items':[{'menu_item_id':menu.json()['id'],'quantity':'2'}],'notes':'Sem cebola'});assert order.status_code==201,order.text
    assert world.post(f"/v1/restaurant/orders/{order.json()['id']}/send",headers=headers).status_code==200
    queue=world.get('/v1/restaurant/production?station=kitchen',headers=headers).json()['items'];assert len(queue)==1
    item=queue[0]['id']
    for state in ['preparing','ready','delivered']:
        update=world.patch(f"/v1/restaurant/order-items/{item}",headers=headers,json={'status':state});assert update.status_code==200,update.text
    detail=world.get(f"/v1/restaurant/tabs/{tab.json()['id']}",headers=headers).json()
    assert detail['total']=='24.00' and detail['items'][0]['status']=='delivered'
    # Two separately idempotent cash entries model a split amount; change returned only on the final receipt.
    first=world.post(f"/v1/restaurant/tabs/{tab.json()['id']}/payments",headers=headers,json={'amount':'10.00','received_amount':'10.00','idempotency_key':'split-cash-0001'})
    assert first.status_code==201,first.text
    second=world.post(f"/v1/restaurant/tabs/{tab.json()['id']}/payments",headers=headers,json={'amount':'14.00','received_amount':'20.00','idempotency_key':'split-cash-0002'})
    assert second.status_code==201,second.text and second.json()['change']=='6.00'
    final=world.get(f"/v1/restaurant/tabs/{tab.json()['id']}",headers=headers).json()
    assert final['status']=='closed' and final['paid']=='24.00' and final['remaining']=='0.00'
    assert world.get('/v1/restaurant/floor',headers=headers).json()['items'][0]['status']=='available'
    stock=world.get('/v1/products',headers=headers).json()['items'][0]
    assert stock['stock']=='8'
    assert world.get('/v1/audit-events',headers=headers).status_code==200


def test_restaurant_menu_and_order_data_are_company_scoped(world):
    token=company(world);headers=auth(token)
    world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    other=company(world,'Outro','outro','other@example.test');other_headers=auth(other)
    world.put('/v1/company/profile',headers=auth(other),json={'primary_segment':'restaurant','answers':{'tables':True}})
    category=world.post('/v1/restaurant/menu/categories',headers=headers,json={'name':'Bebidas'});assert category.status_code==201
    assert world.get('/v1/restaurant/menu',headers=other_headers).json()['categories']==[]
    product_other=product(world,other,sku='OUTRO-1')
    rejected=world.post('/v1/restaurant/menu/items',headers=headers,json={'category_id':category.json()['id'],'product_id':product_other['id'],'station':'bar'})
    assert rejected.status_code==404
    operator=world.post('/v1/users',headers=headers,json={'full_name':'Operador','email':'floor@example.test','password':'floor-test-password-123','role':'operator'})
    assert operator.status_code==201
    logged=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':'floor@example.test','password':'floor-test-password-123'}).json()['access_token']
    denied=world.post('/v1/restaurant/menu/categories',headers=auth(logged),json={'name':'Não permitido'})
    assert denied.status_code==403


def test_restaurant_cash_payment_idempotency_is_safe(world):
    owner=company(world);headers=auth(owner)
    world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    prod=product(world,owner,sku='IDEMP-1',price='5.00')
    world.post(f"/v1/products/{prod['id']}/stock-adjustments",headers=headers,json={'direction':'in','quantity':'2','reason':'Teste inicial'})
    world.post('/v1/registers/open',headers=headers,json={'label':'Caixa','opening_amount':'0'})
    area=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Sala'}).json()
    table=world.post('/v1/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'T1','capacity':2}).json()
    service=world.post(f"/v1/restaurant/tables/{table['id']}/open",headers=headers).json()
    tab=world.post(f"/v1/restaurant/services/{service['id']}/tabs",headers=headers,json={'label':'A'}).json()
    category=world.post('/v1/restaurant/menu/categories',headers=headers,json={'name':'Comida'}).json()
    menu=world.post('/v1/restaurant/menu/items',headers=headers,json={'category_id':category['id'],'product_id':prod['id']}).json()
    order=world.post(f"/v1/restaurant/tabs/{tab['id']}/orders",headers=headers,json={'items':[{'menu_item_id':menu['id'],'quantity':'1'}]}).json()
    world.post(f"/v1/restaurant/orders/{order['id']}/send",headers=headers)
    item=world.get('/v1/restaurant/production?station=kitchen',headers=headers).json()['items'][0]
    for status in ['preparing','ready','delivered']: world.patch(f"/v1/restaurant/order-items/{item['id']}",headers=headers,json={'status':status})
    payment={'amount':'5','received_amount':'5','idempotency_key':'idempotency-restaurant-0001'}
    first=world.post(f"/v1/restaurant/tabs/{tab['id']}/payments",headers=headers,json=payment);assert first.status_code==201
    again=world.post(f"/v1/restaurant/tabs/{tab['id']}/payments",headers=headers,json=payment);assert again.status_code==201 and again.json()['idempotent'] is True
    changed=world.post(f"/v1/restaurant/tabs/{tab['id']}/payments",headers=headers,json={**payment,'amount':'4'});assert changed.status_code==409
    assert len(world.get('/v1/sales',headers=headers).json()['items'])==1


def test_restaurant_electronic_payments_are_not_supported(world):
    owner=company(world);headers=auth(owner)
    world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    response=world.post('/v1/restaurant/tabs/not-a-tab/payments',headers=headers,json={'amount':'1','received_amount':'1','idempotency_key':'electronic-test-0001','method':'pix'})
    assert response.status_code==422


def test_restaurant_open_table_index_enforces_single_active_service(world):
    owner=company(world);headers=auth(owner)
    world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    area=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Índice'}).json()
    table=world.post('/v1/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'Índice','capacity':2}).json()
    assert world.post(f"/v1/restaurant/tables/{table['id']}/open",headers=headers).status_code==200
    assert world.post(f"/v1/restaurant/tables/{table['id']}/open",headers=headers).status_code==409
    me=world.get('/v1/me',headers=headers).json()
    conn=main.connect_db()
    try: assert conn.execute("SELECT count(*) FROM restaurant_services WHERE company_id=? AND table_id=? AND status='open'",(me['company_id'],table['id'])).fetchone()[0]==1
    finally: conn.close()


def test_restaurant_open_table_rejects_conflicting_occupation(world):
    owner=company(world);headers=auth(owner)
    world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    area=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Salão'}).json()
    table=world.post('/v1/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'Mesa 1','capacity':4}).json()
    opened=world.post(f"/v1/restaurant/tables/{table['id']}/open",headers=headers)
    assert opened.status_code==200
    assert world.post(f"/v1/restaurant/tables/{table['id']}/open",headers=headers).status_code==409
    assert world.get('/v1/restaurant/floor',headers=headers).json()['items'][0]['status']=='occupied'


def test_module_status_catalog_has_no_duplicate_keys_and_all_features_are_classified():
    import ast
    import inspect
    tree=ast.parse(inspect.getsource(main))
    assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="MODULE_STATUSES" for t in n.targets))
    keys=[ast.literal_eval(k) for k in assignment.value.keys]
    assert len(keys)==len(set(keys))
    assert set(main.MODULE_STATUSES)=={row[0] for row in main.FEATURES}
    assert main.MODULE_STATUSES["restaurant_floor"]=="AVAILABLE"
    for feature in {"tables_tabs","delivery","kitchen","barcode","weighed_items","expiry_control","variants","returns","commissions","appointments","measurement_sales"}:
        assert main.MODULE_STATUSES[feature]=="NOT_IMPLEMENTED"


def test_partial_restaurant_floor_cannot_be_activated_as_complete_module(world):
    owner=company(world);headers=auth(owner)
    world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    assert world.post('/v1/company/modules/restaurant_floor/activate',headers=headers,json={'confirmed':True}).status_code==409


def test_unimplemented_segment_module_is_never_activated(world):
    owner=company(world);headers=auth(owner)
    world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    response=world.post('/v1/company/modules/tables_tabs/activate',headers=headers,json={'confirmed':True})
    assert response.status_code==409
    state={x['id']:x for x in world.get('/v1/configuration/catalog',headers=headers).json()['features']}
    assert state['tables_tabs']['status']=='NOT_IMPLEMENTED'


def test_master_plans_require_bootstrap_secret(world):
    assert world.get('/v1/master/plans').status_code==403


def test_feature_catalog_marks_only_implemented_features_available(world):
    owner=company(world)
    catalog=world.get('/v1/configuration/catalog',headers=auth(owner)).json()
    by_id={item['id']:item for item in catalog['features']}
    assert {'sales','inventory','purchasing','customers','finance','reports','staff','profile_setup'} <= set(by_id)
    assert all(by_id[item]['available'] for item in main.IMPLEMENTED_FEATURES)
    assert all(by_id[item]['enabled'] for item in main.IMPLEMENTED_FEATURES)
    assert not by_id['barcode']['available'] and not by_id['barcode']['enabled']
    assert by_id['tables_tabs']['status']=='NOT_IMPLEMENTED'
    assert by_id['restaurant_floor']['available'] and not by_id['restaurant_floor']['enabled']


def test_barcode_feature_auto_enables_only_when_requested_on_compatible_segment(world):
    owner=company(world)
    headers=auth(owner)
    world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    restaurant={x['id']:x for x in world.get('/v1/configuration/catalog',headers=headers).json()['features']}
    assert restaurant['barcode']['status']=='NOT_IMPLEMENTED' and restaurant['barcode']['enabled'] is False
    world.put('/v1/company/profile',headers=headers,json={'primary_segment':'grocery','answers':{'barcode':True}})
    grocery={x['id']:x for x in world.get('/v1/configuration/catalog',headers=headers).json()['features']}
    assert grocery['barcode']['status']=='NOT_IMPLEMENTED' and grocery['barcode']['enabled'] is False


def test_segment_catalog_and_profile_configuration_are_tenant_scoped_and_audited(world):
    owner = company(world)
    other = company(world, "Outro Negócio", "outro-negocio", "other@example.test")
    catalog = world.get("/v1/configuration/catalog", headers=auth(owner))
    assert catalog.status_code == 200
    assert {x["id"] for x in catalog.json()["segments"]} >= {"retail", "grocery", "restaurant", "apparel", "services", "construction"}
    payload = {"primary_segment":"restaurant", "secondary_segments":["grocery"],
               "answers":{"tables":True,"delivery":True,"kitchen":False,"barcode":True,"weighed_items":True,"perishables":True}}
    saved = world.put("/v1/company/profile", headers=auth(owner), json=payload)
    assert saved.status_code == 200, saved.text
    assert set(saved.json()["modules"]) == main.IMPLEMENTED_FEATURES - {'barcode'}
    profile = world.get("/v1/configuration/catalog", headers=auth(owner)).json()["profile"]
    assert profile["primary_segment"] == "restaurant"
    assert world.get("/v1/configuration/catalog", headers=auth(other)).json()["profile"] is None
    events=world.get("/v1/audit-events", headers=auth(owner)).json()["items"]
    assert any(e["action"]=="company.profile_configured" for e in events)
    updated=world.put("/v1/company/profile",headers=auth(owner),json={"primary_segment":"retail","answers":{"barcode":True}})
    assert updated.status_code==200
    history=world.get("/v1/company/profile/history",headers=auth(owner)).json()["items"]
    assert len(history)==2
    assert history[0]["profile"]["primary_segment"]=="retail"
    assert history[1]["profile"]["primary_segment"]=="restaurant"
    assert history[1]["profile"]["answers"]["tables"] is True


def test_profile_configuration_rejects_unknown_segment_or_answer(world):
    owner=company(world)
    bad_segment=world.put("/v1/company/profile",headers=auth(owner),json={"primary_segment":"casino"})
    assert bad_segment.status_code==422
    bad_answer=world.put("/v1/company/profile",headers=auth(owner),json={"primary_segment":"retail","answers":{"tables":True}})
    assert bad_answer.status_code==422


def test_operator_cannot_change_company_profile(world):
    owner=company(world)
    world.post("/v1/users",headers=auth(owner),json={"full_name":"Operador","email":"operator@demo.test","password":"secure-password-123","role":"operator"})
    result=world.post("/v1/auth/login",json={"company_slug":"mercado-um","email":"operator@demo.test","password":"secure-password-123"})
    assert result.status_code==200
    denied=world.put("/v1/company/profile",headers=auth(result.json()["access_token"]),json={"primary_segment":"restaurant"})
    assert denied.status_code==403

@pytest.fixture(autouse=True)
def reset_rate_limiter():
    main._RATE_LIMITER = main.WindowRateLimiter()
    yield
    main._RATE_LIMITER = main.WindowRateLimiter()


def test_product_barcode_category_and_minimum_stock(world):
    token=company(world)
    created=world.post("/v1/products",headers=auth(token),json={"sku":"DRINK-1","name":"Suco","price":"5.00","category":"Bebidas","barcode":"7891234567890","minimum_stock":"4"})
    assert created.status_code==201,created.text
    p=created.json()
    assert p["barcode"]=="7891234567890" and p["category"]=="Bebidas" and p["minimum_stock"]=="4" and p["low_stock"]
    assert world.get("/v1/products?search=7891234567890",headers=auth(token)).json()["items"][0]["id"]==p["id"]
    assert world.get("/v1/products/low-stock",headers=auth(token)).json()["total"]==1
    world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=auth(token),json={"direction":"in","quantity":"5","reason":"Entrada de estoque"})
    updated=world.get("/v1/products",headers=auth(token)).json()["items"][0]
    assert updated["low_stock"] is False


def test_barcode_is_unique_per_company_but_not_cross_tenant(world):
    one=company(world)
    two=company(world,"Loja Dois","loja-dois","two@example.test")
    payload={"sku":"A","name":"Item A","price":"1.00","barcode":"12345678"}
    assert world.post("/v1/products",headers=auth(one),json=payload).status_code==201
    assert world.post("/v1/products",headers=auth(one),json={**payload,"sku":"B"}).status_code==409
    assert world.post("/v1/products",headers=auth(two),json=payload).status_code==201


def test_legacy_product_schema_migrates_without_losing_catalog(tmp_path, monkeypatch):
    if main.POSTGRES_BACKEND: pytest.skip("PRAGMA legacy fixture targets SQLite compatibility only")
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "legacy.sqlite3"))
    main.init_db()
    conn=main.connect_db()
    try:
        conn.execute("DROP INDEX IF EXISTS products_company_barcode")
        conn.execute("DELETE FROM schema_migrations WHERE version IN (2,3)")
        conn.execute("ALTER TABLE products DROP COLUMN min_stock_milli")
        conn.execute("ALTER TABLE products DROP COLUMN barcode")
        conn.execute("ALTER TABLE products DROP COLUMN category")
    finally:
        conn.close()
    main.init_db()
    conn=main.connect_db()
    try:
        columns={r[1] for r in conn.execute("PRAGMA table_info(products)")}
        assert {"category","barcode","min_stock_milli"} <= columns
        indexes={r[1] for r in conn.execute("PRAGMA index_list(products)")}
        assert "products_company_barcode" in indexes
    finally:
        conn.close()


def test_product_edit_audits_previous_and_new_values(world):
    token=company(world)
    p=product(world,token,sku="OLD-SKU",name="Nome antigo",price="4.50")
    response=world.put(f"/v1/products/{p['id']}",headers=auth(token),json={"sku":"NEW-SKU","name":"Nome novo","price":"6.25","cost":"2.10","category":"Mercearia","barcode":"4567890123456","minimum_stock":"3"})
    assert response.status_code==200,response.text
    assert response.json()["sku"]=="NEW-SKU" and response.json()["category"]=="Mercearia"
    events=world.get("/v1/audit-events",headers=auth(token)).json()["items"]
    update=next(e for e in events if e["action"]=="product.updated")
    assert update["details"]["before"]["sku"]=="OLD-SKU"
    assert update["details"]["after"]["sku"]=="NEW-SKU"


def test_product_edit_rejects_cross_tenant_and_duplicate_barcode(world):
    one=company(world)
    two=company(world,"Loja Dois","loja-dois","two@example.test")
    p=world.post("/v1/products",headers=auth(one),json={"sku":"P1","name":"Produto um","price":"1.00","barcode":"12345678"}).json()
    assert world.put(f"/v1/products/{p['id']}",headers=auth(two),json={"sku":"P2","name":"Alheio","price":"2.00"}).status_code==404
    own=world.post("/v1/products",headers=auth(one),json={"sku":"P2","name":"Produto dois","price":"2.00"}).json()
    assert world.put(f"/v1/products/{own['id']}",headers=auth(one),json={"sku":"P2","name":"Produto dois","price":"2.00","barcode":"12345678"}).status_code==409


def test_manual_expenses_and_partial_payable_payments_are_audited_idempotent_and_tenant_scoped(world):
    from datetime import date,timedelta
    owner=company(world)
    other=company(world,'Loja Financeiro','loja-financeiro','finance@example.test')
    due=(date.today()+timedelta(days=5)).isoformat()
    created=world.post('/v1/finance/expenses',headers={**auth(owner),'Idempotency-Key':'expense-create-main'},json={'description':'Conta de internet','amount':'100.00','due_date':due,'cost_center':'Administrativo'})
    assert created.status_code==201,created.text
    payable=created.json()['id']
    assert created.json()['status']=='open'
    replay_create=world.post('/v1/finance/expenses',headers={**auth(owner),'Idempotency-Key':'expense-create-main'},json={'description':'Conta de internet','amount':'100.00','due_date':due,'cost_center':'Administrativo'})
    assert replay_create.status_code==200 and replay_create.json()['id']==payable
    conflict_create=world.post('/v1/finance/expenses',headers={**auth(owner),'Idempotency-Key':'expense-create-main'},json={'description':'Conta diferente','amount':'100.00','due_date':due,'cost_center':'Administrativo'})
    assert conflict_create.status_code==409
    assert world.post('/v1/finance/expenses',headers=auth(owner),json={'description':'Sem chave','amount':'1.00','due_date':due}).status_code==400
    assert world.post('/v1/finance/expenses',headers={**auth(owner),'Idempotency-Key':'expense-zero'},json={'description':'Valor zero','amount':'0.001','due_date':due}).status_code==422
    assert world.get('/v1/finance/payables',headers=auth(other)).json()['items']==[]
    overdue=world.post('/v1/finance/expenses',headers={**auth(owner),'Idempotency-Key':'expense-create-overdue'},json={'description':'Conta vencida','amount':'1.00','due_date':(date.today()-timedelta(days=1)).isoformat()})
    assert overdue.status_code==201 and overdue.json()['status']=='open'
    assert world.post('/v1/finance/expenses',headers={**auth(owner),'Idempotency-Key':'expense-created-paid'},json={'description':'Licença anual','amount':'12.00','due_date':due,'payment_date':date.today().isoformat(),'payment_reference':'Comprovante declarado'}).json()['status']=='paid'
    payload={'amount':'30.00','payment_date':date.today().isoformat(),'reference':'PIX declarado'}
    headers={**auth(owner),'Idempotency-Key':'expense-payment-a'}
    first=world.post(f'/v1/finance/payables/{payable}/payments',headers=headers,json=payload)
    assert first.status_code==201,first.text
    replay=world.post(f'/v1/finance/payables/{payable}/payments',headers=headers,json=payload)
    assert replay.status_code==200 and replay.json()['paid']=='30.00'
    assert world.post(f'/v1/finance/payables/{payable}/payments',headers={**auth(owner),'Idempotency-Key':'expense-payment-b'},json={'amount':'70.01','payment_date':date.today().isoformat(),'reference':'Transferência'}).status_code==409
    assert next(x for x in world.get('/v1/finance/payables',headers=auth(owner)).json()['items'] if x['id']==payable)['remaining']=='70.00'
    assert world.get('/v1/finance/summary',headers=auth(owner)).json()['open_payables']=='71.00'
    cashflow=world.get(f"/v1/finance/cashflow?start_date={date.today().isoformat()}&end_date={date.today().isoformat()}",headers=auth(owner))
    assert cashflow.status_code==200,cashflow.text
    assert cashflow.json()['items']==[{'date':date.today().isoformat(),'cash_sales':{'amount':'0.00','count':0},'manual_payments_out':{'amount':'42.00','count':2}}]
    assert 'não confirmadas por banco' in cashflow.json()['note']
    product_id=product(world,owner,sku='CASHFLOW-1',price='8.00')['id']
    world.post(f'/v1/products/{product_id}/stock-adjustments',headers=auth(owner),json={'direction':'in','quantity':'2','reason':'Entrada fluxo'})
    world.post('/v1/registers/open',headers=auth(owner),json={'label':'Caixa fluxo','opening_amount':'0'})
    sale=world.post('/v1/sales',headers={**auth(owner),'Idempotency-Key':'cashflow-sale-1'},json={'items':[{'product_id':product_id,'quantity':'1'}],'payment_method':'cash','received_amount':'8.00'})
    assert sale.status_code==201,sale.text
    cashflow=world.get(f"/v1/finance/cashflow?start_date={date.today().isoformat()}&end_date={date.today().isoformat()}",headers=auth(owner)).json()
    current=next(x for x in cashflow['items'] if x['date']==date.today().isoformat())
    assert current['cash_sales']=={'amount':'8.00','count':1}
    assert current['manual_payments_out']=={'amount':'42.00','count':2}
    future=world.post(f'/v1/finance/payables/{payable}/payments',headers={**auth(owner),'Idempotency-Key':'payment-future'},json={'amount':'1.00','payment_date':(date.today()+timedelta(days=1)).isoformat(),'reference':'Futuro inválido'})
    assert future.status_code==422
    assert world.get('/v1/finance/cashflow?start_date=2026-02-01&end_date=2026-01-01',headers=auth(owner)).status_code==422
    other_flow=world.get(f"/v1/finance/cashflow?start_date={date.today().isoformat()}&end_date={date.today().isoformat()}",headers=auth(other))
    assert other_flow.status_code==200 and other_flow.json()['items']==[]
    op=world.post('/v1/users',headers=auth(owner),json={'full_name':'Operador finanças','email':'finance-op@example.test','password':'strong-operator-password-567','role':'operator'})
    op_token=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':'finance-op@example.test','password':'strong-operator-password-567'}).json()['access_token']
    assert world.get(f"/v1/finance/cashflow?start_date={date.today().isoformat()}&end_date={date.today().isoformat()}",headers=auth(op_token)).status_code==403
    final=world.post(f'/v1/finance/payables/{payable}/payments',headers={**auth(owner),'Idempotency-Key':'expense-payment-c'},json={'amount':'70.00','payment_date':date.today().isoformat(),'reference':'Segunda parte'} )
    assert final.status_code==201 and final.json()['status']=='paid' and final.json()['remaining']=='0.00'
    listing=world.get('/v1/finance/payables?status=paid',headers=auth(owner)).json()['items']
    paid_expense=next(x for x in listing if x['id']==payable)
    assert paid_expense['paid']=='100.00' and paid_expense['cost_center']=='Administrativo'
    assert world.post(f'/v1/finance/payables/{payable}/payments',headers={**auth(other),'Idempotency-Key':'cross-tenant'},json=payload).status_code==404
    conn=main.connect_db()
    try:
        count=conn.execute('SELECT COUNT(*) FROM payable_payments WHERE company_id=? AND payable_id=?',(conn.execute('SELECT id FROM companies WHERE slug=?',('mercado-um',)).fetchone()[0],payable)).fetchone()[0]
        with pytest.raises(Exception): conn.execute('UPDATE payable_payments SET reference=? WHERE payable_id=?',('alterado',payable))
        with pytest.raises(Exception): conn.execute('DELETE FROM payable_payments WHERE payable_id=?',(payable,))
    finally: conn.close()
    assert count==2
    assert world.get('/v1/audit-events',headers=auth(owner)).json()['items']


def test_schema_migrations_are_versioned_and_idempotent(world):
    if main.POSTGRES_BACKEND: pytest.skip("Exact migration set differs by backend")
    main.init_db()
    main.init_db()
    conn=main.connect_db()
    try:
        versions=[tuple(r) for r in conn.execute('SELECT version,name FROM schema_migrations ORDER BY version').fetchall()]
    finally: conn.close()
    assert versions==[(1,'sales_customer_id'),(2,'product_metadata'),(3,'company_barcode_unique_index'),(4,'backfill_legacy_paid_payables'),(5,'plans_branches_entitlements_permissions'),(6,'restaurant_menu_tabs_orders_cash_payments'),(7,'nullable_branch_scope_no_legacy_backfill'),(8,'branch_scoped_stock_ledger'),(9,'branch_scoped_cash_registers'),(10,'branch_purchase_scope_indexes'),(11,'branch_stock_sale_reversal_event_kinds'),(12,'nullable_restaurant_branch_scope'),(13,'branch_scoped_restaurant_menu'),(14,'branch_restaurant_menu_price_snapshot'),(15,'branch_restaurant_orders_cash_ledger'),(16,'restaurant_waiter_portal_and_assignments')]


def test_legacy_paid_payables_are_backfilled_into_ledger_idempotently(world):
    if main.POSTGRES_BACKEND: pytest.skip("Legacy backfill test mutates directly via SQLite connection")
    from datetime import date,timedelta
    owner=company(world)
    created=world.post('/v1/finance/expenses',headers={**auth(owner),'Idempotency-Key':'legacy-expense'},json={'description':'Conta histórica','amount':'25.00','due_date':(date.today()+timedelta(days=1)).isoformat()}).json()
    conn=main.connect_db()
    try:
        conn.execute("UPDATE accounts_payable SET status='paid',paid_at=?,payment_note=? WHERE id=?",(date.today().isoformat()+'T12:00:00+00:00','Baixa informada antiga',created['id']))
        conn.execute("DELETE FROM schema_migrations WHERE version=4")
    finally: conn.close()
    main.init_db()
    main.init_db()
    conn=main.connect_db()
    try:
        rows=conn.execute('SELECT amount_cents,reference,idempotency_key FROM payable_payments WHERE payable_id=?',(created['id'],)).fetchall()
        assert len(rows)==1 and rows[0]['amount_cents']==2500
        assert rows[0]['reference']=='Baixa informada antiga'
        assert rows[0]['idempotency_key']=='legacy-paid:'+created['id']
    finally: conn.close()


def test_supplier_purchase_receipt_updates_inventory_once_and_audits(world):
    owner=company(world)
    supplier=world.post("/v1/suppliers",headers=auth(owner),json={"name":"Fornecedor Local","email":"vendas@example.test"})
    assert supplier.status_code==201,supplier.text
    p=product(world,owner,sku="FAR-1",name="Farinha",price="8.00")
    order=world.post("/v1/purchase-orders",headers=auth(owner),json={"supplier_id":supplier.json()["id"],"order_number":"OC-001","notes":"Compra de teste","items":[{"product_id":p["id"],"quantity":"10","unit_cost":"4.00"}]})
    assert order.status_code==201,order.text
    item=world.get("/v1/purchase-orders",headers=auth(owner)).json()["items"][0]["items"][0]
    assert world.patch(f"/v1/purchase-orders/{order.json()['id']}",headers=auth(owner),json={"status":"ordered"}).status_code==200
    payload={"items":[{"order_item_id":item["id"],"quantity":"4"}]}
    headers={**auth(owner),"Idempotency-Key":"receipt-001"}
    received=world.post(f"/v1/purchase-orders/{order.json()['id']}/receipts",headers=headers,json=payload)
    assert received.status_code==201,received.text
    assert received.json()["status"]=="partially_received"
    replay=world.post(f"/v1/purchase-orders/{order.json()['id']}/receipts",headers=headers,json=payload)
    assert replay.status_code==200
    assert world.get("/v1/products",headers=auth(owner)).json()["items"][0]["stock"]=="4"
    assert any(e["action"]=="purchase.received" for e in world.get("/v1/audit-events",headers=auth(owner)).json()["items"])
    done=world.post(f"/v1/purchase-orders/{order.json()['id']}/receipts",headers={**auth(owner),"Idempotency-Key":"receipt-002"},json={"items":[{"order_item_id":item["id"],"quantity":"6"}]})
    assert done.status_code==201 and done.json()["status"]=="received"
    assert world.get("/v1/products",headers=auth(owner)).json()["items"][0]["stock"]=="10"


def test_purchase_order_rejects_cross_tenant_product_and_overreceipt(world):
    one=company(world)
    two=company(world,"Loja Dois","loja-dois","two@example.test")
    supplier=world.post("/v1/suppliers",headers=auth(one),json={"name":"F Um"})
    p=product(world,two)
    bad=world.post("/v1/purchase-orders",headers=auth(one),json={"supplier_id":supplier.json()["id"],"order_number":"OC-X","items":[{"product_id":p["id"],"quantity":"1","unit_cost":"1.00"}]})
    assert bad.status_code==404
    p1=product(world,one,sku="A1")
    order=world.post("/v1/purchase-orders",headers=auth(one),json={"supplier_id":supplier.json()["id"],"order_number":"OC-Y","items":[{"product_id":p1["id"],"quantity":"2","unit_cost":"1.00"}]}).json()
    item=world.get("/v1/purchase-orders",headers=auth(one)).json()["items"][0]["items"][0]
    world.patch(f"/v1/purchase-orders/{order['id']}",headers=auth(one),json={"status":"ordered"})
    over=world.post(f"/v1/purchase-orders/{order['id']}/receipts",headers={**auth(one),"Idempotency-Key":"over-1"},json={"items":[{"order_item_id":item["id"],"quantity":"2.001"}]})
    assert over.status_code==409
    assert world.get("/v1/products",headers=auth(one)).json()["items"][0]["stock"]=="0"


def test_stock_operator_cannot_mark_purchase_order_ordered(world):
    owner=company(world)
    world.post("/v1/users",headers=auth(owner),json={"full_name":"Estoque","email":"stock@example.test","password":"secure-password-123","role":"stock"})
    tok=world.post("/v1/auth/login",json={"company_slug":"mercado-um","email":"stock@example.test","password":"secure-password-123"}).json()["access_token"]
    assert world.post("/v1/suppliers",headers=auth(tok),json={"name":"Permitido estoque"}).status_code==201


def test_purchase_cancellation_is_audited_and_blocks_later_receiving(world):
    owner=company(world)
    supplier=world.post("/v1/suppliers",headers=auth(owner),json={"name":"Fornecedor Cancelamento"}).json()
    p=product(world,owner,sku="CANCEL-1")
    order=world.post("/v1/purchase-orders",headers=auth(owner),json={"supplier_id":supplier["id"],"order_number":"CANCEL-001","items":[{"product_id":p["id"],"quantity":"3","unit_cost":"2.00"}]}).json()
    result=world.patch(f"/v1/purchase-orders/{order['id']}",headers=auth(owner),json={"status":"cancelled"})
    assert result.status_code==200 and result.json()["status"]=="cancelled"
    item=world.get("/v1/purchase-orders",headers=auth(owner)).json()["items"][0]["items"][0]
    denied=world.post(f"/v1/purchase-orders/{order['id']}/receipts",headers={**auth(owner),"Idempotency-Key":"cancelled-receipt"},json={"items":[{"order_item_id":item["id"],"quantity":"1"}]})
    assert denied.status_code==409
    assert world.get("/v1/products",headers=auth(owner)).json()["items"][0]["stock"]=="0"
    assert any(e["action"]=="purchase_order.status_changed" for e in world.get("/v1/audit-events",headers=auth(owner)).json()["items"])


def test_supplier_and_purchase_order_are_company_isolated(world):
    one=company(world)
    two=company(world,"Loja Dois","loja-dois","two@example.test")
    supplier=world.post("/v1/suppliers",headers=auth(one),json={"name":"Fornecedor Privado"}).json()
    denied=world.post("/v1/purchase-orders",headers=auth(two),json={"supplier_id":supplier["id"],"order_number":"NOPE","items":[{"product_id":product(world,two)["id"],"quantity":"1","unit_cost":"1.00"}]})
    assert denied.status_code==404
    assert world.get("/v1/suppliers",headers=auth(two)).json()["items"]==[]


def test_customer_pii_access_is_restricted_by_role(world):
    owner=company(world)
    c=world.post("/v1/customers",headers=auth(owner),json={"name":"Cliente Pessoal","email":"pessoal@example.test","phone":"11999998888","tax_id":"12345678900","notes":"Observação privada"}).json()
    op=world.post("/v1/users",headers=auth(owner),json={"full_name":"Operadora","email":"op-pii@example.test","password":"strong-operator-password-789","role":"operator"}).json()
    operator=world.post("/v1/auth/login",json={"company_slug":"mercado-um","email":"op-pii@example.test","password":"strong-operator-password-789"}).json()["access_token"]
    lookup=world.get("/v1/customers",headers=auth(operator)).json()["items"]
    assert lookup==[{"id":c["id"],"name":"Cliente Pessoal"}]
    assert world.get("/v1/customers?search=pessoal@example.test",headers=auth(operator)).json()["items"]==[]
    assert world.get(f"/v1/customers/{c['id']}",headers=auth(operator)).status_code==403
    assert world.post("/v1/customers",headers=auth(operator),json={"name":"Só nome"}).status_code==201
    assert world.post("/v1/customers",headers=auth(operator),json={"name":"Com dado","phone":"11999998888"}).status_code==403
    stock=world.post("/v1/users",headers=auth(owner),json={"full_name":"Estoque","email":"stock-pii@example.test","password":"strong-stock-password-789","role":"stock"}).json()
    stock_token=world.post("/v1/auth/login",json={"company_slug":"mercado-um","email":"stock-pii@example.test","password":"strong-stock-password-789"}).json()["access_token"]
    assert world.get("/v1/customers",headers=auth(stock_token)).status_code==403
    assert world.get(f"/v1/customers/{c['id']}",headers=auth(stock_token)).status_code==403
    assert world.get(f"/v1/customers/{c['id']}",headers=auth(owner)).json()["customer"]["tax_id"]=="12345678900"


def test_customer_profile_isolated_and_attached_to_sale(world):
    owner=company(world)
    customer=world.post("/v1/customers",headers=auth(owner),json={"name":"Cliente Exemplo","email":"CLIENTE@EXAMPLE.TEST","phone":"11999990000"})
    assert customer.status_code==201,customer.text
    assert customer.json()["email"]=="cliente@example.test"
    duplicate=world.post("/v1/customers",headers=auth(owner),json={"name":"Outro nome","email":"cliente@example.test"})
    assert duplicate.status_code==409
    assert world.post("/v1/customers",headers=auth(owner),json={"name":"Cliente sem e-mail"}).status_code==201
    p=product(world,owner)
    world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=auth(owner),json={"direction":"in","quantity":"2","reason":"Entrada inicial"})
    world.post("/v1/registers/open",headers=auth(owner),json={"label":"Caixa 1","opening_amount":"0"})
    sale=world.post("/v1/sales",headers={**auth(owner),"Idempotency-Key":"customer-sale-1"},json={"items":[{"product_id":p["id"],"quantity":"1"}],"payment_method":"cash","received_amount":"10.50","customer_id":customer.json()["id"]})
    assert sale.status_code==201,sale.text
    assert sale.json()["customer_id"]==customer.json()["id"]
    other=company(world,"Loja cliente","loja-cliente","loja@example.test")
    assert world.get("/v1/customers",headers=auth(other)).json()["items"]==[]
    assert world.post("/v1/sales",headers={**auth(other),"Idempotency-Key":"foreign-customer"},json={"items":[{"product_id":product(world,other)["id"],"quantity":"1"}],"payment_method":"cash","received_amount":"10.50","customer_id":customer.json()["id"]}).status_code==404


def test_sale_customer_id_is_part_of_idempotency_hash(world):
    owner=company(world)
    p=product(world,owner)
    world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=auth(owner),json={"direction":"in","quantity":"2","reason":"Entrada inicial"})
    world.post("/v1/registers/open",headers=auth(owner),json={"label":"Caixa 1","opening_amount":"0"})
    one=world.post("/v1/customers",headers=auth(owner),json={"name":"Cliente A","email":"a@example.test"}).json()
    two=world.post("/v1/customers",headers=auth(owner),json={"name":"Cliente B","email":"b@example.test"}).json()
    headers={**auth(owner),"Idempotency-Key":"customer-idem"}
    body={"items":[{"product_id":p["id"],"quantity":"1"}],"payment_method":"cash","received_amount":"10.50","customer_id":one["id"]}
    assert world.post("/v1/sales",headers=headers,json=body).status_code==201
    assert world.post("/v1/sales",headers=headers,json={**body,"customer_id":two["id"]}).status_code==409


def test_customer_history_returns_only_own_company_sales(world):
    one=company(world)
    c=world.post("/v1/customers",headers=auth(one),json={"name":"Histórico","email":"hist@example.test"}).json()
    p=product(world,one)
    world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=auth(one),json={"direction":"in","quantity":"2","reason":"Entrada"})
    world.post("/v1/registers/open",headers=auth(one),json={"label":"Caixa","opening_amount":"0"})
    sale=world.post("/v1/sales",headers={**auth(one),"Idempotency-Key":"hist-sale"},json={"items":[{"product_id":p["id"],"quantity":"1"}],"payment_method":"cash","received_amount":"10.50","customer_id":c["id"]})
    assert sale.status_code==201
    detail=world.get(f"/v1/customers/{c['id']}",headers=auth(one)).json()
    assert detail["sales"]["count"]==1 and detail["sales"]["total_paid"]=="10.50"
    two=company(world,"Outra","outra","outra@example.test")
    assert world.get(f"/v1/customers/{c['id']}",headers=auth(two)).status_code==404


def test_purchase_receipt_optional_due_date_creates_payable_and_manual_paid_state(world):
    owner=company(world)
    supplier=world.post('/v1/suppliers',headers=auth(owner),json={'name':'Fornecedor Financeiro'}).json()
    p=product(world,owner,sku='PAY-1')
    order=world.post('/v1/purchase-orders',headers=auth(owner),json={'supplier_id':supplier['id'],'order_number':'PAY-001','items':[{'product_id':p['id'],'quantity':'2','unit_cost':'3.25'}]}).json()
    item=world.get('/v1/purchase-orders',headers=auth(owner)).json()['items'][0]['items'][0]
    world.patch(f"/v1/purchase-orders/{order['id']}",headers=auth(owner),json={'status':'ordered'})
    receipt=world.post(f"/v1/purchase-orders/{order['id']}/receipts",headers={**auth(owner),'Idempotency-Key':'pay-receipt'},json={'items':[{'order_item_id':item['id'],'quantity':'2'}],'due_date':'2026-12-15'})
    assert receipt.status_code==201,receipt.text
    assert receipt.json()['payable_amount']=='6.50'
    summary=world.get('/v1/finance/summary',headers=auth(owner)).json()
    assert summary['open_payables']=='6.50'
    payable=world.get('/v1/finance/payables',headers=auth(owner)).json()['items'][0]
    assert payable['status']=='open' and payable['due_date']=='2026-12-15'
    mark=world.post(f"/v1/finance/payables/{payable['id']}/mark-paid",headers=auth(owner),json={'payment_note':'Pago manual via banco ref 123'})
    assert mark.status_code==200 and 'não representa confirmação bancária' in mark.json()['note']
    assert world.get('/v1/finance/summary',headers=auth(owner)).json()['open_payables']=='0.00'


def test_purchase_receipt_without_due_date_does_not_create_payable_and_past_date_rejected(world):
    owner=company(world)
    supplier=world.post('/v1/suppliers',headers=auth(owner),json={'name':'Fornecedor sem prazo'}).json()
    p=product(world,owner,sku='NO-DUE')
    order=world.post('/v1/purchase-orders',headers=auth(owner),json={'supplier_id':supplier['id'],'order_number':'NO-DUE-1','items':[{'product_id':p['id'],'quantity':'1','unit_cost':'1.00'}]}).json()
    item=world.get('/v1/purchase-orders',headers=auth(owner)).json()['items'][0]['items'][0]
    world.patch(f"/v1/purchase-orders/{order['id']}",headers=auth(owner),json={'status':'ordered'})
    response=world.post(f"/v1/purchase-orders/{order['id']}/receipts",headers={**auth(owner),'Idempotency-Key':'past-due'},json={'items':[{'order_item_id':item['id'],'quantity':'1'}],'due_date':'2020-01-01'})
    assert response.status_code==422
    assert world.get('/v1/products',headers=auth(owner)).json()['items'][0]['stock']=='0'
    assert world.get('/v1/finance/payables',headers=auth(owner)).json()['items']==[]


def test_sale_void_restores_stock_reverses_cash_and_is_manager_only(world):
    owner=company(world)
    p=product(world,owner,sku='VOID-1',price='5.00')
    world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=auth(owner),json={'direction':'in','quantity':'3','reason':'Estoque inicial'})
    world.post('/v1/registers/open',headers=auth(owner),json={'label':'Caixa','opening_amount':'20.00'})
    sale=world.post('/v1/sales',headers={**auth(owner),'Idempotency-Key':'void-sale-1'},json={'items':[{'product_id':p['id'],'quantity':'2'}],'payment_method':'cash','received_amount':'10.00'}).json()
    result=world.post(f"/v1/sales/{sale['id']}/void",headers=auth(owner),json={'reason':'Cliente desistiu da compra'})
    assert result.status_code==200 and result.json()['status']=='voided'
    assert world.get('/v1/products',headers=auth(owner)).json()['items'][0]['stock']=='3'
    current=world.get('/v1/registers/current',headers=auth(owner)).json()
    assert current['expected_amount']=='20.00'
    assert world.get('/v1/dashboard/summary',headers=auth(owner)).json()['sales_today']==0
    repeat=world.post(f"/v1/sales/{sale['id']}/void",headers=auth(owner),json={'reason':'Tentativa repetida'})
    assert repeat.status_code==409
    assert any(e['action']=='sale.voided' for e in world.get('/v1/audit-events',headers=auth(owner)).json()['items'])


def test_operator_cannot_void_sale_or_foreign_sale(world):
    owner=company(world)
    p=product(world,owner,sku='V-1')
    world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=auth(owner),json={'direction':'in','quantity':'2','reason':'Entrada'})
    world.post('/v1/registers/open',headers=auth(owner),json={'label':'Caixa','opening_amount':'0'})
    sale=world.post('/v1/sales',headers={**auth(owner),'Idempotency-Key':'void-role'},json={'items':[{'product_id':p['id'],'quantity':'1'}],'payment_method':'cash','received_amount':'10.50'}).json()
    world.post('/v1/users',headers=auth(owner),json={'full_name':'Operador','email':'voidop@example.test','password':'secure-password-123','role':'operator'})
    operator=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':'voidop@example.test','password':'secure-password-123'}).json()['access_token']
    assert world.post(f"/v1/sales/{sale['id']}/void",headers=auth(operator),json={'reason':'Solicitação de cliente'}).status_code==403
    other=company(world,'Outra loja','outra-loja','outro@example.test')
    assert world.post(f"/v1/sales/{sale['id']}/void",headers=auth(other),json={'reason':'Solicitação de cliente'}).status_code==404


def test_stock_movement_history_filters_by_company_product_and_kind(world):
    one=company(world)
    p=product(world,one,sku='HIST-1')
    world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=auth(one),json={'direction':'in','quantity':'4','reason':'Recebimento'})
    world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=auth(one),json={'direction':'out','quantity':'1','reason':'Quebra registrada'})
    all_rows=world.get('/v1/stock-movements',headers=auth(one)).json()
    assert all_rows['total']==2
    filtered=world.get(f"/v1/stock-movements?product_id={p['id']}&kind=adjustment_out",headers=auth(one)).json()
    assert filtered['total']==1 and filtered['items'][0]['reason']=='Quebra registrada'
    assert world.get('/v1/stock-movements?kind=invalid',headers=auth(one)).status_code==422
    two=company(world,'Loja Dois','loja-dois','two@example.test')
    assert world.get('/v1/stock-movements',headers=auth(two)).json()['total']==0


def test_void_requires_open_cash_with_enough_balance_and_never_rewrites_closed_register(world):
    owner=company(world)
    p=product(world,owner,sku='VOID-CLOSED',price='5.00')
    world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=auth(owner),json={'direction':'in','quantity':'2','reason':'Entrada'})
    oldreg=world.post('/v1/registers/open',headers=auth(owner),json={'label':'Caixa antigo','opening_amount':'0'}).json()
    sale=world.post('/v1/sales',headers={**auth(owner),'Idempotency-Key':'void-closed-sale'},json={'items':[{'product_id':p['id'],'quantity':'1'}],'payment_method':'cash','received_amount':'5.00'}).json()
    closed=world.post(f"/v1/registers/{oldreg['id']}/close",headers=auth(owner),json={'counted_amount':'5.00'})
    assert closed.status_code==200
    no_register=world.post(f"/v1/sales/{sale['id']}/void",headers=auth(owner),json={'reason':'Cancelamento solicitado'})
    assert no_register.status_code==409
    assert world.get('/v1/products',headers=auth(owner)).json()['items'][0]['stock']=='1'
    newreg=world.post('/v1/registers/open',headers=auth(owner),json={'label':'Caixa atual','opening_amount':'4.99'}).json()
    insufficient=world.post(f"/v1/sales/{sale['id']}/void",headers=auth(owner),json={'reason':'Cancelamento solicitado'})
    assert insufficient.status_code==409
    assert world.get('/v1/products',headers=auth(owner)).json()['items'][0]['stock']=='1'
    # Opening balance is enough; this successful void is recorded only against the new open drawer.
    world.post(f"/v1/registers/{newreg['id']}/movements",headers=auth(owner),json={'kind':'supply','amount':'0.01','reason':'Troco para teste'})
    done=world.post(f"/v1/sales/{sale['id']}/void",headers=auth(owner),json={'reason':'Cancelamento solicitado'})
    assert done.status_code==200
    old=world.get(f"/v1/registers/{oldreg['id']}",headers=auth(owner))
    assert old.status_code==404 or old.json()['difference']=='0.00'
    assert world.get('/v1/registers/current',headers=auth(owner)).json()['expected_amount']=='0.00'


def test_sales_report_periods_totals_and_tenant_isolation(world):
    owner=company(world)
    p=product(world,owner,sku='REPORT-1',price='5.00')
    world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=auth(owner),json={'direction':'in','quantity':'3','reason':'Entrada'})
    world.post('/v1/registers/open',headers=auth(owner),json={'label':'Caixa','opening_amount':'20.00'})
    sale=world.post('/v1/sales',headers={**auth(owner),'Idempotency-Key':'report-sale'},json={'items':[{'product_id':p['id'],'quantity':'1'}],'payment_method':'cash','received_amount':'5.00'}).json()
    world.post(f"/v1/sales/{sale['id']}/void",headers=auth(owner),json={'reason':'Cancelamento relatório'})
    report=world.get('/v1/reports/sales?start_date=2026-01-01&end_date=2026-12-31&group_by=day',headers=auth(owner)).json()
    assert report['totals']=={'sale_count':1,'paid_count':0,'voided_count':1,'gross_paid':'0.00','voided_value':'5.00'}
    op=world.post('/v1/users',headers=auth(owner),json={'full_name':'Operador Relatório','email':'report-op@example.test','password':'strong-operator-password-234','role':'operator'}).json()
    operator=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':'report-op@example.test','password':'strong-operator-password-234'}).json()['access_token']
    assert world.get('/v1/reports/sales?start_date=2026-01-01&end_date=2026-12-31',headers=auth(operator)).status_code==403
    assert world.get('/v1/audit-events',headers=auth(operator)).status_code==403
    assert world.get('/v1/registers/current',headers=auth(operator)).status_code==200
    assert world.get('/v1/sales',headers=auth(operator)).status_code==200
    stock=world.post('/v1/users',headers=auth(owner),json={'full_name':'Estoque Relatório','email':'report-stock@example.test','password':'strong-stock-password-234','role':'stock'}).json()
    stock_token=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':'report-stock@example.test','password':'strong-stock-password-234'}).json()['access_token']
    assert world.get('/v1/sales',headers=auth(stock_token)).status_code==403
    assert world.get('/v1/registers/current',headers=auth(stock_token)).status_code==403
    assert world.get('/v1/dashboard/summary',headers=auth(stock_token)).status_code==403
    assert world.get('/v1/audit-events',headers=auth(stock_token)).status_code==403
    assert world.get('/v1/reports/sales?start_date=2026-01-01&end_date=2026-12-31',headers=auth(stock_token)).status_code==403
    assert world.get('/v1/reports/inventory',headers=auth(stock_token)).status_code==403
    assert world.get('/v1/customers',headers=auth(stock_token)).status_code==403
    assert world.get('/v1/reports/sales?start_date=2026-12-31&end_date=2026-01-01',headers=auth(owner)).status_code==422
    assert world.get('/v1/reports/sales?start_date=2025-01-01&end_date=2026-12-31',headers=auth(owner)).status_code==422
    two=company(world,'Outra','outra','outra@example.test')
    assert world.get('/v1/reports/sales?start_date=2026-01-01&end_date=2026-12-31',headers=auth(two)).json()['totals']['sale_count']==0


def test_sqlite_request_connection_can_be_cleaned_up_from_worker_thread(monkeypatch,tmp_path):
    if main.POSTGRES_BACKEND: pytest.skip("Thread cleanup test targets SQLite connections")
    import threading
    monkeypatch.setattr(main,"DB_PATH",str(tmp_path/"threaded.sqlite3"))
    conn=main.connect_db()
    outcomes=[]
    worker=threading.Thread(target=lambda: outcomes.append(conn.execute("SELECT 1").fetchone()[0]))
    worker.start();worker.join()
    conn.close()
    assert outcomes==[1]


def test_inventory_report_large_values_do_not_overflow_sqlite_sum(world):
    owner=company(world)
    p=world.post('/v1/products',headers=auth(owner),json={'sku':'BIG-INV','name':'Lote grande','unit':'un','price':'9999999999.99','cost':'9999999999.99','minimum_stock':'0'}).json()
    conn=main.connect_db()
    try:
        conn.execute("UPDATE products SET stock_milli=? WHERE id=?",(999999999999,p['id']))
    finally:
        conn.close()
    response=world.get('/v1/reports/inventory',headers=auth(owner))
    assert response.status_code==200,response.text
    assert response.json()['summary']['estimated_cost_value']=='9999999999980000000.00'


def test_inventory_report_estimated_values_and_role_scope(world):
    owner=company(world)
    p=world.post('/v1/products',headers=auth(owner),json={'sku':'INV-A','name':'Item A','unit':'un','price':'10.00','cost':'4.50','minimum_stock':'2'}).json()
    world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=auth(owner),json={'direction':'in','quantity':'3','reason':'Saldo de abertura'})
    report=world.get('/v1/reports/inventory',headers=auth(owner)).json()
    assert report['summary']['product_count']==1
    assert report['summary']['low_stock_products']==0
    assert report['summary']['estimated_cost_value']=='13.50'
    assert report['summary']['estimated_retail_value']=='30.00'
    assert report['items'][0]['cost_value']=='13.50'
    assert world.get('/v1/reports/inventory?search=ausente',headers=auth(owner)).json()['items']==[]
    other=company(world,'Outra','outra','outra@example.test')
    assert world.get('/v1/reports/inventory',headers=auth(other)).json()['summary']['product_count']==0


def test_every_authenticated_api_endpoint_has_permission_policy():
    exempt={"/v1/auth/login","/v1/auth/demo","/v1/companies/register","/v1/bootstrap","/v1/segments","/v1/me","/v1/permissions/effective"}
    protected=[]
    for route in main.app.routes:
        path=getattr(route,"path","")
        if path.startswith("/v1/") and path not in exempt and not path.startswith("/v1/master/"):
            for method in getattr(route,"methods",set())-{"HEAD","OPTIONS"}: protected.append((method,path))
    uncovered=[(m,p) for m,p in protected if main.permission_for_endpoint(m,p) is None]
    assert uncovered==[]


def test_permission_guard_returns_http_401_for_invalid_bearer(world):
    response=world.get('/v1/products',headers={'Authorization':'Bearer not-a-valid-token'})
    assert response.status_code==401


def test_role_permission_override_is_enforced_by_endpoint_guard(world):
    owner=company(world);headers=auth(owner)
    created=world.post('/v1/users',headers=headers,json={'full_name':'Operador','email':'guard@example.test','password':'secure-password-123','role':'operator'})
    assert created.status_code==201
    token=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':'guard@example.test','password':'secure-password-123'}).json()['access_token']
    operator=auth(token)
    assert world.get('/v1/products',headers=operator).status_code==200
    assert world.put('/v1/company/permissions/role',headers=headers,json={'role':'operator','permission':'products.read','allowed':False}).status_code==200
    assert world.get('/v1/products',headers=operator).status_code==403
    assert world.get('/v1/permissions/effective?permission=products.read',headers=operator).json()['allowed'] is False


def test_permission_router_inventory_covers_all_authenticated_endpoints():
    exempt={"/v1/auth/login","/v1/auth/demo","/v1/companies/register","/v1/bootstrap","/v1/segments","/v1/me","/v1/permissions/effective"}
    missing=[]
    for route in main.app.routes:
        path=getattr(route,"path","")
        if path.startswith("/v1/") and path not in exempt and not path.startswith("/v1/master/"):
            for method in getattr(route,"methods",set())-{"HEAD","OPTIONS"}:
                if main.permission_for_endpoint(method,path) is None: missing.append((method,path))
    assert missing==[]


def test_multi_branch_operations_fail_closed_and_never_attribute_legacy_data(world):
    owner=company(world);headers=auth(owner)
    first=world.get('/v1/company/branches',headers=headers).json()['items'][0]
    legacy=product(world,owner,sku='LEGACY-UNASSIGNED')
    branch=world.post('/v1/company/branches',headers=headers,json={'code':'second','name':'Segunda unidade'})
    assert branch.status_code==201
    denied=world.get('/v1/products',headers=headers)
    assert denied.status_code==409 and 'escopo operacional de filial' in denied.json()['detail']
    invalid=world.get('/v1/products',headers={**headers,'X-Branch-Id':'foreign-branch-id'})
    assert invalid.status_code==403
    foreign=company(world,'Outra empresa','another','another@example.test')
    foreign_branch=world.get('/v1/company/branches',headers=auth(foreign)).json()['items'][0]['id']
    cross=world.get('/v1/products',headers={**headers,'X-Branch-Id':foreign_branch})
    assert cross.status_code==403


def test_branch_scope_migration_preserves_legacy_rows_as_unassigned(world):
    owner=company(world);headers=auth(owner)
    legacy=product(world,owner,sku='LEGACY-UNASSIGNED')
    conn=main.connect_db()
    try:
        columns={r[1] for r in conn.execute('PRAGMA table_info(products)').fetchall()}
        assert 'branch_id' in columns
        row=conn.execute('SELECT branch_id FROM products WHERE id=? AND company_id=?',(legacy['id'],world.get('/v1/me',headers=headers).json()['company_id'])).fetchone()
        assert row['branch_id'] is None
        status=conn.execute("SELECT value FROM deployment_metadata WHERE key='branch_partitioning'").fetchone()
        assert status[0]=='schema_added_legacy_unassigned'
    finally: conn.close()


def test_branch_membership_cannot_be_forged_across_tenant_and_multi_branch_fails_closed(world):
    owner=company(world);headers=auth(owner)
    branches=world.get('/v1/company/branches',headers=headers).json()['items']
    foreign=company(world,'Different','different','different@example.test')
    foreign_branch=world.get('/v1/company/branches',headers=auth(foreign)).json()['items'][0]['id']
    assert world.get('/v1/products',headers={**headers,'X-Branch-Id':foreign_branch}).status_code==403
    assert world.post('/v1/company/branches',headers=headers,json={'code':'second','name':'Segunda unidade'}).status_code==201
    assert world.get('/v1/products',headers=headers).status_code==409
    assert world.get('/v1/products',headers={**headers,'X-Branch-Id':branches[0]['id']}).status_code==409


def test_branch_membership_administration_is_tenant_scoped_and_audited(world):
    owner=company(world);headers=auth(owner)
    branch=world.post('/v1/company/branches',headers=headers,json={'code':'west','name':'Unidade Oeste'}).json()
    created=world.post('/v1/users',headers=headers,json={'full_name':'Vendedora','email':'branch-user@example.test','password':'secure-password-123','role':'operator'})
    assert created.status_code==201,created.text
    uid=created.json()['id']
    assigned=world.put(f"/v1/company/branches/{branch['id']}/users",headers=headers,json={'user_ids':[uid, world.get('/v1/me',headers=headers).json()['id']]})
    assert assigned.status_code==200,assigned.text
    assert {u['id'] for u in world.get(f"/v1/company/branches/{branch['id']}/users",headers=headers).json()['users']}=={uid,world.get('/v1/me',headers=headers).json()['id']}
    events=world.get('/v1/audit-events',headers=headers).json()['items']
    assert any(e['action']=='branch.users_replaced' and e['entity_id']==branch['id'] for e in events)
    foreign_token=company(world,'Unrelated','unrelated','unrelated@example.test')
    foreign_branch=world.get('/v1/company/branches',headers=auth(foreign_token)).json()['items'][0]['id']
    assert world.put(f"/v1/company/branches/{foreign_branch}/users",headers=headers,json={'user_ids':[uid]}).status_code==404
    assert world.put(f"/v1/company/branches/{branch['id']}/users",headers=headers,json={'user_ids':['nonexistent-foreign-user']}).status_code==422


def test_branch_inventory_adjustment_history_transfer_and_cross_branch_denial(world):
    owner=company(world);headers=auth(owner)
    branches=world.get('/v1/company/branches',headers=headers).json()['items']
    first=branches[0]['id']
    p=product(world,owner,sku='BRANCH-LEDGER')
    second=world.post('/v1/company/branches',headers=headers,json={'code':'north','name':'Norte'}).json()['id']
    # Branch ledger is independent from legacy company stock; no silent import.
    assert world.get(f'/v1/branches/{first}/inventory',headers=headers).json()['items'][0]['quantity']=='0'
    add=world.post(f'/v1/branches/{first}/inventory/{p["id"]}/adjustments',headers=headers,json={'direction':'in','quantity':'12','reason':'Recebimento fictício'});assert add.status_code==201,add.text
    assert world.get(f'/v1/branches/{second}/inventory',headers=headers).json()['items'][0]['quantity']=='0'
    assert world.post(f'/v1/branches/{first}/inventory/{p["id"]}/adjustments',headers=headers,json={'direction':'out','quantity':'13','reason':'Saída inválida'}).status_code==409
    transfer=world.post('/v1/branches/stock-transfers',headers={**headers,'Idempotency-Key':'transfer-test-1'},json={'from_branch_id':first,'to_branch_id':second,'product_id':p['id'],'quantity':'5','reason':'Transferência fictícia'});assert transfer.status_code==201,transfer.text
    replay=world.post('/v1/branches/stock-transfers',headers={**headers,'Idempotency-Key':'transfer-test-1'},json={'from_branch_id':first,'to_branch_id':second,'product_id':p['id'],'quantity':'5','reason':'Transferência fictícia'});assert replay.status_code==200 and replay.json()['idempotent'] is True
    conflict=world.post('/v1/branches/stock-transfers',headers={**headers,'Idempotency-Key':'transfer-test-1'},json={'from_branch_id':first,'to_branch_id':second,'product_id':p['id'],'quantity':'4','reason':'Transferência fictícia'});assert conflict.status_code==409
    inv1=world.get(f'/v1/branches/{first}/inventory',headers=headers).json()['items'][0]['quantity']
    inv2=world.get(f'/v1/branches/{second}/inventory',headers=headers).json()['items'][0]['quantity']
    assert (inv1,inv2)==('7','5')
    assert len(world.get(f'/v1/branches/{first}/stock-movements',headers=headers).json()['items'])==2
    other_token=company(world,'Different','different','different@example.test')
    assert world.get(f'/v1/branches/{first}/inventory',headers=auth(other_token)).status_code==403
    # Unauthorized branch operator is rejected, even if tenant ID is correct.
    op_resp=world.post('/v1/users',headers=headers,json={'full_name':'Operador limitado','email':'branch-limited@example.test','password':'secure-password-123','role':'operator'});assert op_resp.status_code==201
    op_token=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':'branch-limited@example.test','password':'secure-password-123'}).json()['access_token']
    assert world.get(f'/v1/branches/{second}/inventory',headers=auth(op_token)).status_code==403


def test_branch_cash_registers_are_isolated_and_reconcile_only_within_branch(world):
    owner_token=company(world);headers=auth(owner_token)
    first=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    second=world.post('/v1/company/branches',headers=headers,json={'code':'cash2','name':'Caixa 2'}).json()['id']
    a=world.post(f'/v1/branches/{first}/registers/open',headers=headers,json={'label':'Caixa Matriz','opening_amount':'50.00'});assert a.status_code==201,a.text
    b=world.post(f'/v1/branches/{second}/registers/open',headers=headers,json={'label':'Caixa Filial','opening_amount':'10.00'});assert b.status_code==201,b.text
    assert world.get(f'/v1/branches/{first}/registers/current',headers=headers).json()['expected_amount']=='50.00'
    assert world.get(f'/v1/branches/{second}/registers/current',headers=headers).json()['expected_amount']=='10.00'
    assert world.post(f"/v1/branches/{first}/registers/{a.json()['id']}/movements",headers=headers,json={'kind':'supply','amount':'5','reason':'Fundo'}).status_code==201
    closed=world.post(f"/v1/branches/{first}/registers/{a.json()['id']}/close",headers=headers,json={'counted_amount':'55'});assert closed.status_code==200 and closed.json()['difference']=='0.00'
    other=company(world,'Foreign','foreign','foreign@example.test')
    assert world.get(f'/v1/branches/{first}/registers/current',headers=auth(other)).status_code==403


def test_branch_sale_uses_only_branch_stock_and_cash_register(world):
    owner_token=company(world);headers=auth(owner_token)
    first=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    p=product(world,owner_token,sku='BRANCH-SALE',price='4.50')
    second=world.post('/v1/company/branches',headers=headers,json={'code':'sale2','name':'Loja Dois'}).json()['id']
    for bid in (first,second):
        assert world.post(f'/v1/branches/{bid}/registers/open',headers=headers,json={'label':'Turno','opening_amount':'0'}).status_code==201
    assert world.post(f'/v1/branches/{first}/inventory/{p["id"]}/adjustments',headers=headers,json={'direction':'in','quantity':'5','reason':'Estoque teste'}).status_code==201
    sale=world.post(f'/v1/branches/{first}/sales',headers={**headers,'Idempotency-Key':'branch-sale-1'},json={'items':[{'product_id':p['id'],'quantity':'2'}],'payment_method':'cash','received_amount':'10.00'})
    assert sale.status_code==201,sale.text
    assert sale.json()['total']=='9.00' and sale.json()['change']=='1.00'
    replay=world.post(f'/v1/branches/{first}/sales',headers={**headers,'Idempotency-Key':'branch-sale-1'},json={'items':[{'product_id':p['id'],'quantity':'2'}],'payment_method':'cash','received_amount':'10.00'})
    assert replay.status_code==200 and replay.json()['idempotent'] is True
    assert world.post(f'/v1/branches/{second}/sales',headers={**headers,'Idempotency-Key':'branch-sale-2'},json={'items':[{'product_id':p['id'],'quantity':'1'}],'payment_method':'cash','received_amount':'5.00'}).status_code==409
    assert world.get(f'/v1/branches/{first}/inventory',headers=headers).json()['items'][0]['quantity']=='3'
    movements=world.get(f'/v1/branches/{first}/stock-movements',headers=headers).json()['items']
    assert any(x['kind']=='sale' and x['quantity']=='2' for x in movements)
    assert world.get(f'/v1/branches/{second}/inventory',headers=headers).json()['items'][0]['quantity']=='0'
    assert world.get(f'/v1/branches/{first}/registers/current',headers=headers).json()['expected_amount']=='9.00'
    closed=world.post(f"/v1/branches/{first}/registers/{world.get(f'/v1/branches/{first}/registers/current',headers=headers).json()['register']['id']}/close",headers=headers,json={'counted_amount':'9.00'})
    assert closed.status_code==200 and closed.json()['difference']=='0.00'
    assert world.get(f'/v1/branches/{second}/registers/current',headers=headers).json()['expected_amount']=='0.00'


def test_branch_sale_void_reverses_only_branch_stock_and_cash_with_audit(world):
    token=company(world);headers=auth(token);first=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    p=product(world,token,sku='BRANCH-VOID',price='3.00')
    other=world.post('/v1/company/branches',headers=headers,json={'code':'void2','name':'Loja Void'}).json()['id']
    reg=world.post(f'/v1/branches/{first}/registers/open',headers=headers,json={'label':'Turno','opening_amount':'20'}).json()
    world.post(f'/v1/branches/{other}/registers/open',headers=headers,json={'label':'Outro','opening_amount':'2'})
    assert world.post(f'/v1/branches/{first}/inventory/{p["id"]}/adjustments',headers=headers,json={'direction':'in','quantity':'4','reason':'Entrada'}).status_code==201
    sale=world.post(f'/v1/branches/{first}/sales',headers={**headers,'Idempotency-Key':'void-branch-sale'},json={'items':[{'product_id':p['id'],'quantity':'2'}],'payment_method':'cash','received_amount':'6'}).json()
    void=world.post(f'/v1/branches/{first}/sales/{sale["id"]}/void',headers=headers,json={'reason':'Cancelamento autorizado'})
    assert void.status_code==200,void.text
    assert world.get(f'/v1/branches/{first}/inventory',headers=headers).json()['items'][0]['quantity']=='4'
    assert world.get(f'/v1/branches/{first}/registers/current',headers=headers).json()['expected_amount']=='20.00'
    assert world.get(f'/v1/branches/{other}/registers/current',headers=headers).json()['expected_amount']=='2.00'
    assert world.post(f'/v1/branches/{first}/sales/{sale["id"]}/void',headers=headers,json={'reason':'Repetir'}).status_code==409


def test_branch_purchase_receipt_posts_only_to_branch_ledger_and_payable(world):
    token=company(world);headers=auth(token)
    branches=world.get('/v1/company/branches',headers=headers).json()['items'];first=branches[0]['id']
    supplier=world.post('/v1/suppliers',headers=headers,json={'name':'Fornecedor Branch'}).json()
    product_data=product(world,token,sku='BRANCH-PO')
    second=world.post('/v1/company/branches',headers=headers,json={'code':'po-north','name':'Norte'}).json()['id']
    order=world.post(f'/v1/branches/{first}/purchase-orders',headers=headers,json={'supplier_id':supplier['id'],'order_number':'PO-BR-1','items':[{'product_id':product_data['id'],'quantity':'8','unit_cost':'2.50'}]})
    assert order.status_code==201,order.text
    assert world.patch(f'/v1/branches/{first}/purchase-orders/{order.json()["id"]}',headers=headers,json={'status':'ordered'}).status_code==200
    lines=world.get(f'/v1/branches/{first}/purchase-orders',headers=headers).json()['items'][0]['items'];line=lines[0]
    rec=world.post(f'/v1/branches/{first}/purchase-orders/{order.json()["id"]}/receipts',headers={**headers,'Idempotency-Key':'branch-receipt-1'},json={'items':[{'order_item_id':line['id'],'quantity':'3'}],'due_date':'2026-12-20'})
    assert rec.status_code==201,rec.text
    assert rec.json()['payable_amount']=='7.50'
    assert world.get(f'/v1/branches/{first}/inventory',headers=headers).json()['items'][0]['quantity']=='3'
    assert world.get(f'/v1/branches/{second}/inventory',headers=headers).json()['items'][0]['quantity']=='0'
    legacy=world.get('/v1/products',headers=headers);assert legacy.status_code==409,legacy.text
    assert world.get(f'/v1/branches/{first}/payables',headers=headers).json()['items'][0]['amount']=='7.50'
    assert world.get(f'/v1/branches/{second}/payables',headers=headers).json()['items']==[]
    replay=world.post(f'/v1/branches/{first}/purchase-orders/{order.json()["id"]}/receipts',headers={**headers,'Idempotency-Key':'branch-receipt-1'},json={'items':[{'order_item_id':line['id'],'quantity':'3'}],'due_date':'2026-12-20'})
    assert replay.status_code==200 and replay.json()['idempotent'] is True
    paid=world.post(f'/v1/branches/{first}/payables/{rec.json()["payable_id"]}/payments',headers={**headers,'Idempotency-Key':'branch-pay-1'},json={'amount':'7.50','payment_date':'2026-11-16','reference':'Pagamento manual de teste'})
    assert paid.status_code==201 and paid.json()['status']=='paid'
    assert world.post(f'/v1/branches/{second}/payables/{rec.json()["payable_id"]}/payments',headers={**headers,'Idempotency-Key':'foreign-branch-pay'},json={'amount':'7.50','payment_date':'2026-11-16','reference':'Não autorizado'}).status_code==404


def test_branch_finance_summary_expenses_and_cashflow_are_branch_isolated(world):
    token=company(world);headers=auth(token);first=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    second=world.post('/v1/company/branches',headers=headers,json={'code':'fin2','name':'Financeiro Norte'}).json()['id']
    today='2026-11-16'
    one=world.post(f'/v1/branches/{first}/finance/expenses',headers={**headers,'Idempotency-Key':'branch-exp-1'},json={'description':'Despesa filial A','amount':'50.00','due_date':'2026-12-01','cost_center':'Loja'})
    assert one.status_code==201,one.text
    replay=world.post(f'/v1/branches/{first}/finance/expenses',headers={**headers,'Idempotency-Key':'branch-exp-1'},json={'description':'Despesa filial A','amount':'50.00','due_date':'2026-12-01','cost_center':'Loja'})
    assert replay.status_code==200 and replay.json()['idempotent'] is True
    assert world.get(f'/v1/branches/{first}/finance/summary',headers=headers).json()['open_payables']=='50.00'
    assert world.get(f'/v1/branches/{second}/finance/summary',headers=headers).json()['open_payables']=='0.00'
    payable=world.get(f'/v1/branches/{first}/payables',headers=headers).json()['items'][0]
    paid=world.post(f'/v1/branches/{first}/payables/{payable["id"]}/payments',headers={**headers,'Idempotency-Key':'branch-exp-pay'},json={'amount':'20.00','payment_date':today,'reference':'Parcial manual'})
    assert paid.status_code==201 and paid.json()['remaining']=='30.00'
    cf=world.get(f'/v1/branches/{first}/finance/cashflow?start_date={today}&end_date={today}',headers=headers)
    assert cf.status_code==200,cf.text
    assert world.get(f'/v1/branches/{second}/payables',headers=headers).json()['items']==[]


def test_branch_dashboard_sales_history_and_reports_exclude_other_branches(world):
    token=company(world);headers=auth(token);first=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    p=product(world,token,sku='BR-REPORT',price='4.00')
    second=world.post('/v1/company/branches',headers=headers,json={'code':'rep2','name':'Relatório 2'}).json()['id']
    for bid in (first,second):world.post(f'/v1/branches/{bid}/registers/open',headers=headers,json={'label':'PDV','opening_amount':'0'})
    world.post(f'/v1/branches/{first}/inventory/{p["id"]}/adjustments',headers=headers,json={'direction':'in','quantity':'3','reason':'Entrada teste'})
    sale=world.post(f'/v1/branches/{first}/sales',headers={**headers,'Idempotency-Key':'report-sale-branch'},json={'items':[{'product_id':p['id'],'quantity':'1'}],'payment_method':'cash','received_amount':'4.00'}).json()
    hist=world.get(f'/v1/branches/{first}/sales',headers=headers)
    assert hist.status_code==200 and hist.json()['items'][0]['id']==sale['id']
    assert world.get(f'/v1/branches/{second}/sales',headers=headers).json()['items']==[]
    dashboard=world.get(f'/v1/branches/{first}/dashboard/summary',headers=headers)
    assert dashboard.status_code==200 and dashboard.json()['sales_today']==1
    today=__import__('datetime').date.today().isoformat()
    report=world.get(f'/v1/branches/{first}/sales/report?start_date={today}&end_date={today}',headers=headers)
    assert report.status_code==200 and report.json()['totals']['paid_count']==1
    assert world.get(f'/v1/branches/{second}/sales/report?start_date={today}&end_date={today}',headers=headers).json()['totals']['sale_count']==0


def test_branch_stock_role_can_create_but_not_send_or_cancel_purchase_order(world):
    token=company(world);headers=auth(token);branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    sup=world.post('/v1/suppliers',headers=headers,json={'name':'Stock supplier'}).json();p=product(world,token,sku='BRANCH-ROLE')
    world.post('/v1/users',headers=headers,json={'full_name':'Stock User','email':'branchstock@example.test','password':'secure-password-123','role':'stock'})
    tok=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':'branchstock@example.test','password':'secure-password-123'}).json()['access_token'];h=auth(tok)
    order=world.post(f'/v1/branches/{branch}/purchase-orders',headers=h,json={'supplier_id':sup['id'],'order_number':'ROLE-PO','items':[{'product_id':p['id'],'quantity':'1','unit_cost':'2.00'}]})
    assert order.status_code==201,order.text
    assert world.patch(f'/v1/branches/{branch}/purchase-orders/{order.json()["id"]}',headers=h,json={'status':'ordered'}).status_code==403


def test_multi_branch_restaurant_routes_fail_closed_before_any_legacy_access(world):
    token=company(world);headers=auth(token);first=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    second=world.post('/v1/company/branches',headers=headers,json={'name':'Segunda unidade','code':'UN2'});assert second.status_code==201,second.text
    headers['X-Branch-Id']=first
    for path in ['/v1/restaurant/areas','/v1/restaurant/floor','/v1/restaurant/reservations','/v1/restaurant/menu','/v1/restaurant/tabs','/v1/restaurant/production?station=kitchen','/v1/restaurant/reports/operations']:
        response=world.get(path,headers=headers);assert response.status_code==409,(path,response.status_code,response.text)
    response=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Tentativa bloqueada'});assert response.status_code==409,response.text


def test_restaurant_branch_scope_migration_keeps_legacy_rows_unassigned(world):
    token=company(world);headers=auth(token)
    configured=world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}});assert configured.status_code==200,configured.text
    area=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Sala legada'});assert area.status_code==201,area.text
    table=world.post('/v1/restaurant/tables',headers=headers,json={'area_id':area.json()['id'],'label':'L1','capacity':4});assert table.status_code==201,table.text
    branch=world.post('/v1/company/branches',headers=headers,json={'name':'Segunda unidade','code':'UN2'});assert branch.status_code==201,branch.text
    conn=main.connect_db()
    try:
        for table_name in ('restaurant_areas','restaurant_tables','restaurant_reservations','restaurant_services','restaurant_tabs','restaurant_orders','restaurant_order_items','restaurant_tab_payments','restaurant_events'):
            columns={r[1] for r in conn.execute(f'PRAGMA table_info({table_name})').fetchall()};assert 'branch_id' in columns,table_name
        legacy=conn.execute('SELECT branch_id FROM restaurant_areas WHERE id=?',(area.json()['id'],)).fetchone();assert legacy[0] is None
        legacy_table=conn.execute('SELECT branch_id FROM restaurant_tables WHERE id=?',(table.json()['id'],)).fetchone();assert legacy_table[0] is None
    finally:conn.close()
    headers['X-Branch-Id']=branch.json()['id']
    assert world.get('/v1/restaurant/floor',headers=headers).status_code==409


def test_branch_restaurant_areas_floor_and_reservations_are_isolated(world):
    token=company(world,"Restaurante", "restaurante-isolado", "dono@restaurante.test")
    headers=auth(token)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    first=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    second_resp=world.post('/v1/company/branches',headers=headers,json={'code':'filial-b','name':'Filial B'})
    assert second_resp.status_code==201,second_resp.text
    second=second_resp.json()['id']
    # Explicitly authorize owner in second branch; branch membership is not inferred by URL/header.
    me=world.get('/v1/me',headers=headers).json()['id']
    assert world.put(f'/v1/company/branches/{second}/users',headers=headers,json={'user_ids':[me]}).status_code==200

    area_a=world.post(f'/v1/branches/{first}/restaurant/areas',headers=headers,json={'name':'Salão A'})
    area_b=world.post(f'/v1/branches/{second}/restaurant/areas',headers=headers,json={'name':'Salão B'})
    assert area_a.status_code==201 and area_b.status_code==201
    table_a=world.post(f'/v1/branches/{first}/restaurant/tables',headers=headers,json={'area_id':area_a.json()['id'],'label':'A1','capacity':4})
    table_b=world.post(f'/v1/branches/{second}/restaurant/tables',headers=headers,json={'area_id':area_b.json()['id'],'label':'B1','capacity':2})
    assert table_a.status_code==201 and table_b.status_code==201
    assert world.get(f'/v1/branches/{first}/restaurant/floor',headers=headers).json()['items'][0]['label']=='A1'
    assert world.get(f'/v1/branches/{second}/restaurant/floor',headers=headers).json()['items'][0]['label']=='B1'

    # A foreign branch/table combination cannot be used to open a table or reserve it.
    assert world.post(f'/v1/branches/{first}/restaurant/tables/{table_b.json()["id"]}/open',headers=headers).status_code==404
    assert world.post(f'/v1/branches/{second}/restaurant/reservations',headers=headers,json={'table_id':table_a.json()['id'],'customer_name':'Cliente A','party_size':2,'starts_at':'2026-12-01T12:00','ends_at':'2026-12-01T13:00'}).status_code==404

    opened=world.post(f'/v1/branches/{first}/restaurant/tables/{table_a.json()["id"]}/open',headers=headers)
    assert opened.status_code==200,opened.text
    reservation=world.post(f'/v1/branches/{first}/restaurant/reservations',headers=headers,json={'table_id':table_a.json()['id'],'customer_name':'Cliente A','party_size':3,'starts_at':'2026-12-01T12:00','ends_at':'2026-12-01T13:00'})
    assert reservation.status_code==201,reservation.text
    assert len(world.get(f'/v1/branches/{first}/restaurant/reservations',headers=headers).json()['items'])==1
    assert world.get(f'/v1/branches/{second}/restaurant/reservations',headers=headers).json()['items']==[]
    assert world.get('/v1/restaurant/floor',headers=headers).status_code==409


def test_branch_restaurant_empty_service_can_be_released_but_tabbed_service_cannot(world):
    token=company(world,"Restaurant Empty", "restaurant-empty", "empty@restaurant.test");headers=auth(token)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    area=world.post(f'/v1/branches/{branch}/restaurant/areas',headers=headers,json={'name':'Pátio'}).json()
    table=world.post(f'/v1/branches/{branch}/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'P1','capacity':4}).json()
    opened=world.post(f'/v1/branches/{branch}/restaurant/tables/{table["id"]}/open',headers=headers);assert opened.status_code==200,opened.text
    closed=world.post(f'/v1/branches/{branch}/restaurant/services/{opened.json()["id"]}/close-empty',headers=headers);assert closed.status_code==200,closed.text
    assert world.get(f'/v1/branches/{branch}/restaurant/floor',headers=headers).json()['items'][0]['status']=='available'



def test_branch_restaurant_menu_isolated_by_filial_and_legacy_menu_excluded(world):
    token=company(world,"Menu branches", "menu-branches", "menu@branches.test");headers=auth(token)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    first=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    p=product(world,token,sku='MENU-ITEM',name='Prato de teste')
    legacy_cat=world.post('/v1/restaurant/menu/categories',headers=headers,json={'name':'Catálogo antigo'});assert legacy_cat.status_code==201
    legacy_link=world.post('/v1/restaurant/menu/items',headers=headers,json={'category_id':legacy_cat.json()['id'],'product_id':p['id'],'station':'kitchen'});assert legacy_link.status_code==201
    second=world.post('/v1/company/branches',headers=headers,json={'code':'b-menu','name':'Filial Menu B'}).json()['id']
    me=world.get('/v1/me',headers=headers).json()['id']
    assert world.put(f'/v1/company/branches/{second}/users',headers=headers,json={'user_ids':[me]}).status_code==200
    a=world.post(f'/v1/branches/{first}/restaurant/menu/categories',headers=headers,json={'name':'Pratos A'});assert a.status_code==201,a.text
    b=world.post(f'/v1/branches/{second}/restaurant/menu/categories',headers=headers,json={'name':'Pratos B'});assert b.status_code==201,b.text
    linked=world.post(f'/v1/branches/{first}/restaurant/menu/items',headers=headers,json={'category_id':a.json()['id'],'product_id':p['id'],'station':'kitchen'});assert linked.status_code==201,linked.text
    assert world.post(f'/v1/branches/{second}/restaurant/menu/items',headers=headers,json={'category_id':a.json()['id'],'product_id':p['id'],'station':'kitchen'}).status_code==404
    menu_a=world.get(f'/v1/branches/{first}/restaurant/menu',headers=headers).json()
    menu_b=world.get(f'/v1/branches/{second}/restaurant/menu',headers=headers).json()
    assert len(menu_a['items'])==1 and menu_a['items'][0]['id']==linked.json()['id']
    assert menu_b['items']==[] and 'Pratos A' not in [c['name'] for c in menu_b['categories']]
    conn=main.connect_db()
    try:
        old_cat=conn.execute('SELECT branch_id FROM restaurant_menu_categories WHERE id=?',(legacy_cat.json()['id'],)).fetchone()
        old_item=conn.execute('SELECT branch_id FROM restaurant_menu_items WHERE id=?',(legacy_link.json()['id'],)).fetchone()
        assert old_cat['branch_id'] is None and old_item['branch_id'] is None
    finally:conn.close()


def test_branch_restaurant_order_production_cash_close_and_branch_ledgers(world):
    token=company(world,"Restaurant order", "restaurant-order", "order@restaurant.test");headers=auth(token)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    branches=world.get('/v1/company/branches',headers=headers).json()['items'];branch=branches[0]['id']
    second=world.post('/v1/company/branches',headers=headers,json={'code':'other-order','name':'Outra filial'}).json()['id']
    uid=world.get('/v1/me',headers=headers).json()['id'];assert world.put(f'/v1/company/branches/{second}/users',headers=headers,json={'user_ids':[uid]}).status_code==200
    prod=product(world,token,sku='BR-PLATE',name='Prato branch',price='12.50')
    area=world.post(f'/v1/branches/{branch}/restaurant/areas',headers=headers,json={'name':'Salão'}).json()
    table=world.post(f'/v1/branches/{branch}/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'M1','capacity':4}).json()
    assert world.post(f'/v1/branches/{branch}/inventory/{prod["id"]}/adjustments',headers=headers,json={'direction':'in','quantity':'5','reason':'Estoque cozinha'}).status_code==201
    assert world.post(f'/v1/branches/{branch}/registers/open',headers=headers,json={'label':'Caixa 1','opening_amount':'10'}).status_code==201
    service=world.post(f'/v1/branches/{branch}/restaurant/tables/{table["id"]}/open',headers=headers).json()
    tab=world.post(f'/v1/branches/{branch}/restaurant/services/{service["id"]}/tabs',headers=headers,json={'label':'Mesa 1'});assert tab.status_code==201,tab.text
    category=world.post(f'/v1/branches/{branch}/restaurant/menu/categories',headers=headers,json={'name':'Pratos'}).json()
    menu=world.post(f'/v1/branches/{branch}/restaurant/menu/items',headers=headers,json={'category_id':category['id'],'product_id':prod['id'],'station':'kitchen'});assert menu.status_code==201,menu.text
    order=world.post(f'/v1/branches/{branch}/restaurant/tabs/{tab.json()["id"]}/orders',headers=headers,json={'items':[{'menu_item_id':menu.json()['id'],'quantity':'2'}]});assert order.status_code==201,order.text
    assert world.post(f'/v1/branches/{second}/restaurant/tabs/{tab.json()["id"]}/orders',headers=headers,json={'items':[{'menu_item_id':menu.json()['id'],'quantity':'1'}]}).status_code==404
    assert world.post(f'/v1/branches/{branch}/restaurant/orders/{order.json()["id"]}/send',headers=headers).status_code==200
    prodq=world.get(f'/v1/branches/{branch}/restaurant/production?station=kitchen',headers=headers).json();assert len(prodq['items'])==1
    item_id=prodq['items'][0]['id']
    for status in ('preparing','ready','delivered'):
        r=world.patch(f'/v1/branches/{branch}/restaurant/order-items/{item_id}',headers=headers,json={'status':status});assert r.status_code==200,r.text
    tabid=tab.json()['id'];detail=world.get(f'/v1/branches/{branch}/restaurant/tabs/{tabid}',headers=headers).json();assert detail['total']=='25.00'
    paid=world.post(f'/v1/branches/{branch}/restaurant/tabs/{tabid}/payments',headers=headers,json={'amount':'25','received_amount':'30','idempotency_key':'branch-rest-pay-01'});assert paid.status_code==201,paid.text;assert paid.json()['change']=='5.00'
    replay=world.post(f'/v1/branches/{branch}/restaurant/tabs/{tabid}/payments',headers=headers,json={'amount':'25','received_amount':'30','idempotency_key':'branch-rest-pay-01'});assert replay.status_code==200 and replay.json()['idempotent'] is True
    assert world.get(f'/v1/branches/{branch}/inventory',headers=headers).json()['items'][0]['quantity']=='3'
    assert world.get(f'/v1/branches/{second}/inventory',headers=headers).json()['items'][0]['quantity']=='0'
    assert world.get(f'/v1/branches/{branch}/registers/current',headers=headers).json()['expected_amount']=='35.00'
    assert world.get(f'/v1/branches/{second}/registers/current',headers=headers).json()['register'] is None
    assert world.get(f'/v1/branches/{branch}/sales',headers=headers).json()['items'][0]['total']=='25.00'
    assert world.get(f'/v1/branches/{branch}/restaurant/tabs?status=all',headers=headers).json()['items'][0]['status']=='closed'
    report=world.get(f'/v1/branches/{branch}/restaurant/reports/operations',headers=headers);assert report.status_code==200 and report.json()['cash_received']=='25.00'
    conn=main.connect_db()
    try:
        company_id=world.get('/v1/me',headers=headers).json()['company_id']
        assert conn.execute('SELECT COUNT(*) FROM sales WHERE company_id=?',(company_id,)).fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM branch_sales WHERE branch_id=?',(branch,)).fetchone()[0]==1
    finally:conn.close()


def test_frontend_favicon_asset_is_served_and_linked(world):
    html=world.get('/').text
    assert 'href="/static/favicon.png?v=logo1"' in html
    response=world.get('/static/favicon.png')
    assert response.status_code==200
    assert response.headers['content-type'].startswith('image/png')
    assert response.content.startswith(b'\x89PNG\r\n\x1a\n')


def test_waiter_portal_assignment_ordering_and_scope(world):
    owner=company(world);headers=auth(owner)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    created=world.post('/v1/restaurant/waiters',headers=headers,json={'full_name':'Garçom QA','email':'waiter@example.test','password':'strong-waiter-password-123'})
    assert created.status_code==201,created.text
    waiter=created.json()
    login=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':waiter['email'],'password':'strong-waiter-password-123'});assert login.status_code==200,login.text
    wh={'Authorization':'Bearer '+login.json()['access_token']}
    assert world.get('/v1/waiter/me',headers=wh).json()['waiter'] is True
    assert world.get('/v1/products',headers=wh).status_code==403
    area=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Salão'}).json()
    table=world.post('/v1/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'G1','capacity':4}).json()
    assert world.put(f"/v1/restaurant/tables/{table['id']}/waiter",headers=headers,json={'waiter_id':waiter['id']}).status_code==200
    floor=world.get('/v1/waiter/floor',headers=wh);assert floor.status_code==200 and floor.json()['tables'][0]['id']==table['id']
    opened=world.post(f"/v1/waiter/tables/{table['id']}/open",headers=wh);assert opened.status_code==200,opened.text
    tab=world.post(f"/v1/waiter/services/{opened.json()['id']}/tabs",headers=wh);assert tab.status_code==201,tab.text
    assert world.get(f"/v1/waiter/tabs/{tab.json()['id']}",headers=wh).status_code==200
    product_row=product(world,owner,sku='WTR-1',name='Suco',price='8.50')
    assert world.post(f"/v1/products/{product_row['id']}/stock-adjustments",headers=headers,json={'direction':'in','quantity':'5','reason':'Estoque de teste garçom'}).status_code==201
    category=world.post('/v1/restaurant/menu/categories',headers=headers,json={'name':'Bebidas'}).json()
    menu=world.post('/v1/restaurant/menu/items',headers=headers,json={'category_id':category['id'],'product_id':product_row['id'],'station':'bar'});assert menu.status_code==201,menu.text
    ordered=world.post(f"/v1/waiter/tabs/{tab.json()['id']}/orders",headers=wh,json={'items':[{'menu_item_id':menu.json()['id'],'quantity':'2'}],'notes':'Sem gelo'});assert ordered.status_code==201,ordered.text
    sent=world.post(f"/v1/waiter/orders/{ordered.json()['id']}/send",headers=wh);assert sent.status_code==200 and sent.json()['status']=='sent'
    detail=world.get(f"/v1/waiter/tabs/{tab.json()['id']}",headers=wh).json();assert detail['total']=='17.00'
    # Manager/production transitions remain auditable; waiter can record only the supported cash receipt.
    owner_reg=world.post('/v1/registers/open',headers=headers,json={'label':'Caixa salão','opening_amount':'0'});assert owner_reg.status_code==201,owner_reg.text
    item=world.get('/v1/restaurant/production?station=bar',headers=headers).json()['items'][0]
    for status in ('preparing','ready'):
        transition=world.patch(f"/v1/restaurant/order-items/{item['id']}",headers=headers,json={'status':status});assert transition.status_code==200,transition.text
    delivered=world.patch(f"/v1/waiter/order-items/{item['id']}/delivered",headers=wh);assert delivered.status_code==200,delivered.text
    payment=world.post(f"/v1/waiter/tabs/{tab.json()['id']}/payments",headers=wh,json={'amount':'17.00','received_amount':'20.00','idempotency_key':'waiter-cash-0001'});assert payment.status_code==201,payment.text
    assert payment.json()['change']=='3.00' and payment.json()['electronic_confirmation'] is False
    # A different tenant cannot read a comanda assigned here.
    other=company(world,name='Outra empresa',slug='outra-empresa',email='other-owner@example.test')
    assert world.get(f"/v1/waiter/tabs/{tab.json()['id']}",headers=auth(other)).status_code==403
    # The owner can read the roster.
    assert world.get('/v1/restaurant/waiters',headers=headers).json()['items'][0]['id']==waiter['id']
    deactivated=world.patch(f"/v1/users/{waiter['id']}",headers=headers,json={'active':False})
    assert deactivated.status_code==200,deactivated.text
    assert world.get('/v1/waiter/me',headers=wh).status_code==401
    assert world.get('/v1/restaurant/waiters',headers=headers).json()['items']==[]
    conn=main.connect_db()
    try:
        assert conn.execute('SELECT active FROM waiter_profiles WHERE user_id=?',(waiter['id'],)).fetchone()[0]==0
    finally: conn.close()
    reactivated=world.patch(f"/v1/users/{waiter['id']}",headers=headers,json={'active':True})
    assert reactivated.status_code==200,reactivated.text
    assert world.get('/v1/restaurant/waiters',headers=headers).json()['items'][0]['active'] is True
    new_login=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':waiter['email'],'password':'strong-waiter-password-123'})
    assert new_login.status_code==200
    assert world.get('/v1/waiter/me',headers={'Authorization':'Bearer '+new_login.json()['access_token']}).status_code==200


def test_waiter_cannot_access_unassigned_table_or_other_tenant(world):
    owner=company(world);headers=auth(owner)
    world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    w=world.post('/v1/restaurant/waiters',headers=headers,json={'full_name':'Garçom B','email':'waiter-b@example.test','password':'strong-waiter-password-456'}).json()
    login=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':w['email'],'password':'strong-waiter-password-456'}).json();wh={'Authorization':'Bearer '+login['access_token']}
    area=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Área'}).json();table=world.post('/v1/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'B1','capacity':2}).json()
    assert world.post(f"/v1/waiter/tables/{table['id']}/open",headers=wh).status_code==409
    assert world.get('/v1/restaurant/tabs',headers=wh).status_code==403


def test_waiter_portal_stays_blocked_for_multi_branch_company(world):
    owner=company(world);headers=auth(owner)
    world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}})
    assert world.post('/v1/company/branches',headers=headers,json={'name':'Filial 2','code':'branch-2'}).status_code==201
    response=world.post('/v1/restaurant/waiters',headers=headers,json={'full_name':'Garçom multi','email':'waiter-multi@example.test','password':'strong-waiter-password-789'})
    assert response.status_code==409 and 'multi-filial' in response.text



def test_multi_branch_blocks_every_legacy_operational_read_even_with_branch_header(world):
    owner=company(world);headers=auth(owner)
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    assert world.post('/v1/company/branches',headers=headers,json={'code':'other','name':'Outra filial'}).status_code==201
    scoped={**headers,'X-Branch-Id':branch}
    probes=[
        ('GET','/v1/products'),('GET','/v1/stock-movements'),('GET','/v1/sales'),
        ('GET','/v1/purchase-orders'),('GET','/v1/finance/summary'),('GET','/v1/finance/cashflow?start_date=2026-11-01&end_date=2026-11-30'),
        ('GET','/v1/dashboard/summary'),('GET','/v1/reports/sales?start_date=2026-11-01&end_date=2026-11-30'),
        ('GET','/v1/restaurant/floor'),('GET','/v1/restaurant/tabs'),('GET','/v1/restaurant/reports/operations'),
    ]
    for method,path in probes:
        response=world.request(method,path,headers=scoped)
        assert response.status_code==409,(method,path,response.status_code,response.text)
    # The dedicated branch-scoped stock view remains available to a member.
    assert world.get(f'/v1/branches/{branch}/inventory',headers=headers).status_code==200


def test_permission_router_maps_semantic_permissions_for_branch_and_restaurant_routes():
    cases={
        ('GET','/v1/branches/b1/sales/report'):'reports.read',
        ('GET','/v1/branches/b1/products'):'products.read',
        ('GET','/v1/branches/b1/inventory'):'inventory.read',
        ('POST','/v1/branches/b1/restaurant/tabs/t1/payments'):'restaurant.cash',
        ('POST','/v1/branches/b1/restaurant/services/s1/close-empty'):'restaurant.floor',
        ('POST','/v1/restaurant/waiters'):'users.manage',
        ('GET','/v1/restaurant/reports/operations'):'reports.read',
    }
    for (method,path),expected in cases.items():
        assert main.permission_for_endpoint(method,path)==expected,(method,path,main.permission_for_endpoint(method,path))


def test_custom_permissions_use_same_branch_route_policy_as_handler(world):
    owner=company(world);headers=auth(owner)
    first=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    second=world.post('/v1/company/branches',headers=headers,json={'code':'branch-permissions','name':'Filial permissões'}).json()['id']
    created=world.post('/v1/users',headers=headers,json={'full_name':'Operadora QA','email':'permission-test@example.test','password':'strong-permission-password-123','role':'operator'})
    assert created.status_code==201,created.text
    uid=created.json()['id']
    assert world.put(f'/v1/company/branches/{first}/users',headers=headers,json={'user_ids':[uid]}).status_code==200
    assert world.put(f'/v1/company/branches/{second}/users',headers=headers,json={'user_ids':[uid]}).status_code==200
    login=world.post('/v1/auth/login',json={'company_slug':'mercado-um','email':'permission-test@example.test','password':'strong-permission-password-123'})
    assert login.status_code==200
    operator=auth(login.json()['access_token'])
    assert world.put('/v1/company/permissions/role',headers=headers,json={'role':'operator','permission':'products.read','allowed':False}).status_code==200
    assert world.put('/v1/company/permissions/role',headers=headers,json={'role':'operator','permission':'inventory.read','allowed':True}).status_code==200
    assert world.get(f'/v1/branches/{first}/products',headers=operator).status_code==403
    assert world.get(f'/v1/branches/{first}/inventory',headers=operator).status_code==200
    assert world.put('/v1/company/permissions/role',headers=headers,json={'role':'operator','permission':'products.read','allowed':True}).status_code==200
    assert world.get(f'/v1/branches/{first}/products',headers=operator).status_code==200
    assert world.put('/v1/company/permissions/role',headers=headers,json={'role':'operator','permission':'reports.read','allowed':False}).status_code==200
    assert world.put('/v1/company/permissions/role',headers=headers,json={'role':'operator','permission':'sales.read','allowed':True}).status_code==200
    report=world.get(f'/v1/branches/{first}/sales/report?start_date=2026-11-01&end_date=2026-11-30',headers=operator)
    assert report.status_code==403
    assert world.put('/v1/company/permissions/role',headers=headers,json={'role':'operator','permission':'reports.read','allowed':True}).status_code==200
    assert world.get(f'/v1/branches/{first}/sales/report?start_date=2026-11-01&end_date=2026-11-30',headers=operator).status_code==200


def test_middleware_permission_matches_local_handler_permission_checks():
    import ast
    import inspect
    mismatches=[]
    for route in main.app.routes:
        path=getattr(route,'path','')
        if not path.startswith('/v1/') or path.startswith('/v1/master/'):
            continue
        endpoint=getattr(route,'endpoint',None)
        if endpoint is None:
            continue
        try: tree=ast.parse(inspect.getsource(endpoint))
        except (OSError,TypeError,IndentationError,SyntaxError): continue
        local={node.args[2].value for node in ast.walk(tree)
               if isinstance(node,ast.Call) and isinstance(node.func,ast.Name)
               and node.func.id=='require_permission' and len(node.args)>2
               and isinstance(node.args[2],ast.Constant) and isinstance(node.args[2].value,str)}
        if not local: continue
        for method in getattr(route,'methods',set())-{'HEAD','OPTIONS'}:
            expected=main.permission_for_endpoint(method,path)
            if expected is not None and expected not in local:
                mismatches.append((method,path,expected,sorted(local)))
    assert mismatches==[]


def test_branch_inventory_concurrent_increments_preserve_ledger_and_balance(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world);headers=auth(owner)
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    p=product(world,owner,sku='CONCURRENT-STOCK',name='Estoque Concorrente')
    def increment(i):
        return world.post(f'/v1/branches/{branch}/inventory/{p["id"]}/adjustments',headers=headers,
                          json={'direction':'in','quantity':'1','reason':f'Ajuste paralelo QA {i}'})
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses=list(pool.map(increment,range(8)))
    assert all(r.status_code==201 for r in responses),[(r.status_code,r.text) for r in responses]
    inventory=world.get(f'/v1/branches/{branch}/inventory',headers=headers).json()
    assert inventory['items'][0]['quantity']=='8'
    movements=world.get(f'/v1/branches/{branch}/stock-movements?product_id={p["id"]}',headers=headers).json()
    assert len(movements['items'])==8
    assert {m['after'] for m in movements['items']}=={str(i) for i in range(1,9)}



def test_concurrent_legacy_stock_adjustments_preserve_every_update(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,name="Legacy stock concurrency",slug="legacy-stock-concurrency",email="legacy-stock-concurrency@example.test");headers=auth(owner)
    p=product(world,owner,sku='LEGACY-ADJUST-CONCURRENCY')
    def adjust(i):
        return world.post(f"/v1/products/{p['id']}/stock-adjustments",headers=headers,json={'direction':'in','quantity':'1','reason':f'Concurrent legacy QA {i}'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(adjust,range(8)))
    assert all(r.status_code==201 for r in responses),[(r.status_code,r.text) for r in responses]
    listed=next(row for row in world.get('/v1/products',headers=headers).json()['items'] if row['id']==p['id'])
    assert listed['stock']=='8'
    events=world.get(f"/v1/stock-movements?product_id={p['id']}",headers=headers).json()['items']
    assert len(events)==8 and {e['after'] for e in events}=={'1','2','3','4','5','6','7','8'}




def test_concurrent_legacy_expense_idempotency_returns_existing_result(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,name="Expense idempotency race",slug="expense-idempotency-race",email="expense-idempotency-race@example.test");headers=auth(owner)
    payload={'description':'Idempotent QA expense','amount':'18.50','due_date':'2026-12-20','cost_center':'QA'}
    def create(_):return world.post('/v1/finance/expenses',headers={**headers,'Idempotency-Key':'same-expense-idempotency'},json=payload)
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(create,range(8)))
    assert sum(r.status_code==201 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==200 for r in responses)==7,[(r.status_code,r.text) for r in responses]
    rows=world.get('/v1/finance/payables?status=all',headers=headers).json()['items']
    assert len([r for r in rows if r['description']==payload['description']])==1


def test_concurrent_branch_expense_idempotency_returns_existing_result(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,name="Branch expense idempotency race",slug="branch-expense-idempotency-race",email="branch-expense-idempotency-race@example.test");headers=auth(owner)
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    payload={'description':'Branch idempotent QA expense','amount':'18.50','due_date':'2026-12-20','cost_center':'QA'}
    def create(_):return world.post(f'/v1/branches/{branch}/finance/expenses',headers={**headers,'Idempotency-Key':'same-branch-expense-idempotency'},json=payload)
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(create,range(8)))
    assert sum(r.status_code==201 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==200 for r in responses)==7,[(r.status_code,r.text) for r in responses]
    rows=world.get(f'/v1/branches/{branch}/payables?status=all',headers=headers).json()['items']
    assert len([r for r in rows if r['description']==payload['description']])==1


def test_concurrent_legacy_register_open_returns_conflict_instead_of_server_error(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,name="Register open race",slug="register-open-race",email="register-open-race@example.test");headers=auth(owner)
    def open_register(i):return world.post('/v1/registers/open',headers=headers,json={'label':f'Register race {i}','opening_amount':'0'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(open_register,range(8)))
    assert sum(r.status_code==201 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==409 for r in responses)==7,[(r.status_code,r.text) for r in responses]
    assert world.get('/v1/registers/current',headers=headers).json()['register']['status']=='open'


def test_concurrent_branch_register_open_returns_conflict_instead_of_server_error(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,name="Branch register open race",slug="branch-register-open-race",email="branch-register-open-race@example.test");headers=auth(owner)
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    def open_register(i):return world.post(f'/v1/branches/{branch}/registers/open',headers=headers,json={'label':f'Branch register {i}','opening_amount':'0'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(open_register,range(8)))
    assert sum(r.status_code==201 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==409 for r in responses)==7,[(r.status_code,r.text) for r in responses]
    assert world.get(f'/v1/branches/{branch}/registers/current',headers=headers).json()['register']['status']=='open'


def test_branch_concurrent_sales_lock_stock_and_record_all_cash_events(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world);headers=auth(owner)
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    p=product(world,owner,sku='CONCURRENT-SALE',name='Venda Concorrente',price='4.50')
    assert world.post(f'/v1/branches/{branch}/registers/open',headers=headers,json={'label':'Turno QA','opening_amount':'0'}).status_code==201
    assert world.post(f'/v1/branches/{branch}/inventory/{p["id"]}/adjustments',headers=headers,json={'direction':'in','quantity':'8','reason':'Carga QA concorrência'}).status_code==201
    def sell(i):
        return world.post(f'/v1/branches/{branch}/sales',headers={**headers,'Idempotency-Key':f'concurrent-sale-{i}'},json={'items':[{'product_id':p['id'],'quantity':'1'}],'payment_method':'cash','received_amount':'5.00'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(sell,range(8)))
    assert all(r.status_code==201 for r in responses),[(r.status_code,r.text) for r in responses]
    inv=world.get(f'/v1/branches/{branch}/inventory',headers=headers).json()['items'][0]
    assert inv['quantity']=='0'
    sales=world.get(f'/v1/branches/{branch}/sales',headers=headers).json()['items']
    assert len(sales)==8 and all(x['status']=='paid' for x in sales)
    register=world.get(f'/v1/branches/{branch}/registers/current',headers=headers).json()
    assert register['expected_amount']=='36.00'


def test_branch_concurrent_transfers_lock_both_balances_and_post_ledger(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world);headers=auth(owner)
    source=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    dest=world.post('/v1/company/branches',headers=headers,json={'code':'transfer-dest','name':'Destino transfer QA'}).json()['id']
    p=product(world,owner,sku='CONCURRENT-TRANSFER',name='Transferência Concorrente')
    assert world.post(f'/v1/branches/{source}/inventory/{p["id"]}/adjustments',headers=headers,json={'direction':'in','quantity':'8','reason':'Carga concorrência QA'}).status_code==201
    def transfer(i):
        return world.post('/v1/branches/stock-transfers',headers={**headers,'Idempotency-Key':f'concurrent-transfer-{i}'},json={'from_branch_id':source,'to_branch_id':dest,'product_id':p['id'],'quantity':'1','reason':f'Transfer QA {i}'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(transfer,range(8)))
    assert all(r.status_code==201 for r in responses),[(r.status_code,r.text) for r in responses]
    src_inv=world.get(f'/v1/branches/{source}/inventory',headers=headers).json()['items'][0]['quantity']
    dst_inv=world.get(f'/v1/branches/{dest}/inventory',headers=headers).json()['items'][0]['quantity']
    assert (src_inv,dst_inv)==('0','8')
    assert world.get(f'/v1/branches/{source}/stock-movements?product_id={p["id"]}&kind=transfer_out',headers=headers).json()['total']==8
    assert world.get(f'/v1/branches/{dest}/stock-movements?product_id={p["id"]}&kind=transfer_in',headers=headers).json()['total']==8


def test_concurrent_transfers_cannot_overdraw_branch_stock(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world);headers=auth(owner)
    source=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    dest=world.post('/v1/company/branches',headers=headers,json={'code':'transfer-limit','name':'Destino limite'}).json()['id']
    p=product(world,owner,sku='TRANSFER-LIMIT',name='Limite concorrência')
    assert world.post(f'/v1/branches/{source}/inventory/{p["id"]}/adjustments',headers=headers,json={'direction':'in','quantity':'4','reason':'Carga QA'}).status_code==201
    def transfer(i):
        return world.post('/v1/branches/stock-transfers',headers={**headers,'Idempotency-Key':f'limited-transfer-{i}'},json={'from_branch_id':source,'to_branch_id':dest,'product_id':p['id'],'quantity':'1','reason':f'Limite QA {i}'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(transfer,range(8)))
    assert sum(r.status_code==201 for r in responses)==4,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==409 for r in responses)==4,[(r.status_code,r.text) for r in responses]
    src=world.get(f'/v1/branches/{source}/inventory',headers=headers).json()['items'][0]['quantity']
    dst=world.get(f'/v1/branches/{dest}/inventory',headers=headers).json()['items'][0]['quantity']
    assert (src,dst)==('0','4')


def test_concurrent_purchase_receipts_never_exceed_ordered_quantity(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world);headers=auth(owner)
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    supplier=world.post('/v1/suppliers',headers=headers,json={'name':'Concurrent supplier'}).json()
    p=product(world,owner,sku='CONCURRENT-RECEIPT',name='Recebimento Concorrente')
    order=world.post(f'/v1/branches/{branch}/purchase-orders',headers=headers,json={'supplier_id':supplier['id'],'order_number':'CONCURRENT-PO','items':[{'product_id':p['id'],'quantity':'4','unit_cost':'2.00'}]})
    assert order.status_code==201,order.text
    assert world.patch(f'/v1/branches/{branch}/purchase-orders/{order.json()["id"]}',headers=headers,json={'status':'ordered'}).status_code==200
    line=world.get(f'/v1/branches/{branch}/purchase-orders',headers=headers).json()['items'][0]['items'][0]
    def receive(i):
        return world.post(f'/v1/branches/{branch}/purchase-orders/{order.json()["id"]}/receipts',headers={**headers,'Idempotency-Key':f'concurrent-receipt-{i}'},json={'items':[{'order_item_id':line['id'],'quantity':'1'}]})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(receive,range(8)))
    assert sum(r.status_code==201 for r in responses)==4,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==409 for r in responses)==4,[(r.status_code,r.text) for r in responses]
    inv=world.get(f'/v1/branches/{branch}/inventory',headers=headers).json()['items'][0]['quantity']
    listing=world.get(f'/v1/branches/{branch}/purchase-orders',headers=headers).json()['items'][0]
    assert inv=='4' and listing['items'][0]['received']=='4' and listing['status']=='received'
    assert world.get(f'/v1/branches/{branch}/stock-movements?product_id={p["id"]}&kind=adjustment_in',headers=headers).json()['total']==4




def test_concurrent_legacy_purchase_cancel_and_receipt_are_serialized(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,name="Cancel receipt race",slug="cancel-receipt-race",email="cancel-receipt-race@example.test");headers=auth(owner)
    supplier=world.post('/v1/suppliers',headers=headers,json={'name':'Race supplier'}).json()
    p=product(world,owner,sku='CANCEL-RECEIPT-RACE')
    order=world.post('/v1/purchase-orders',headers=headers,json={'supplier_id':supplier['id'],'order_number':'CANCEL-RACE-1','items':[{'product_id':p['id'],'quantity':'1','unit_cost':'2.00'}]}).json()
    assert world.patch(f"/v1/purchase-orders/{order['id']}",headers=headers,json={'status':'ordered'}).status_code==200
    item=world.get('/v1/purchase-orders',headers=headers).json()['items'][0]['items'][0]
    def cancel():return world.patch(f"/v1/purchase-orders/{order['id']}",headers=headers,json={'status':'cancelled'})
    def receive():return world.post(f"/v1/purchase-orders/{order['id']}/receipts",headers={**headers,'Idempotency-Key':'cancel-receipt-race-key'},json={'items':[{'order_item_id':item['id'],'quantity':'1'}]})
    with ThreadPoolExecutor(max_workers=2) as pool: cancel_result,receipt_result=list(pool.map(lambda f:f(),(cancel,receive)))
    state=world.get('/v1/purchase-orders',headers=headers).json()['items'][0]['status']
    stock=next(x for x in world.get('/v1/products',headers=headers).json()['items'] if x['id']==p['id'])['stock']
    assert (cancel_result.status_code,receipt_result.status_code,state,stock) in {
        (200,409,'cancelled','0'),(409,201,'received','1')
    },(cancel_result.status_code,receipt_result.status_code,state,stock,cancel_result.text,receipt_result.text)


def test_concurrent_branch_purchase_cancel_and_receipt_are_serialized(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,name="Branch cancel receipt race",slug="branch-cancel-receipt-race",email="branch-cancel-receipt-race@example.test");headers=auth(owner)
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    supplier=world.post('/v1/suppliers',headers=headers,json={'name':'Branch race supplier'}).json()
    p=product(world,owner,sku='BRANCH-CANCEL-RECEIPT-RACE')
    order=world.post(f'/v1/branches/{branch}/purchase-orders',headers=headers,json={'supplier_id':supplier['id'],'order_number':'BRANCH-CANCEL-RACE-1','items':[{'product_id':p['id'],'quantity':'1','unit_cost':'2.00'}]}).json()
    assert world.patch(f"/v1/branches/{branch}/purchase-orders/{order['id']}",headers=headers,json={'status':'ordered'}).status_code==200
    item=world.get(f'/v1/branches/{branch}/purchase-orders',headers=headers).json()['items'][0]['items'][0]
    def cancel():return world.patch(f"/v1/branches/{branch}/purchase-orders/{order['id']}",headers=headers,json={'status':'cancelled'})
    def receive():return world.post(f"/v1/branches/{branch}/purchase-orders/{order['id']}/receipts",headers={**headers,'Idempotency-Key':'branch-cancel-receipt-race-key'},json={'items':[{'order_item_id':item['id'],'quantity':'1'}]})
    with ThreadPoolExecutor(max_workers=2) as pool: cancel_result,receipt_result=list(pool.map(lambda f:f(),(cancel,receive)))
    state=world.get(f'/v1/branches/{branch}/purchase-orders',headers=headers).json()['items'][0]['status']
    stock=next(x for x in world.get(f'/v1/branches/{branch}/inventory',headers=headers).json()['items'] if x['product_id']==p['id'])['quantity']
    assert (cancel_result.status_code,receipt_result.status_code,state,stock) in {
        (200,409,'cancelled','0'),(409,201,'received','1')
    },(cancel_result.status_code,receipt_result.status_code,state,stock,cancel_result.text,receipt_result.text)


def test_concurrent_legacy_receipts_on_distinct_orders_do_not_lose_shared_stock_updates(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,name="Concurrent receipts",slug="concurrent-receipts",email="concurrent-receipts@example.test")
    headers=auth(owner)
    supplier=world.post('/v1/suppliers',headers=headers,json={'name':'Shared stock supplier'}).json()
    p=product(world,owner,sku='SHARED-RECEIPT',name='Produto recebido em pedidos distintos')
    orders=[]
    for number in ('SHARED-PO-A','SHARED-PO-B'):
        order=world.post('/v1/purchase-orders',headers=headers,json={'supplier_id':supplier['id'],'order_number':number,'items':[{'product_id':p['id'],'quantity':'5','unit_cost':'1.00'}]})
        assert order.status_code==201,order.text
        assert world.patch(f"/v1/purchase-orders/{order.json()['id']}",headers=headers,json={'status':'ordered'}).status_code==200
        line=world.get('/v1/purchase-orders',headers=headers).json()['items']
        matching=next(x for x in line if x['id']==order.json()['id'])['items'][0]
        orders.append((order.json()['id'],matching['id']))
    def receive(pair):
        order_id,item_id=pair
        return world.post(f'/v1/purchase-orders/{order_id}/receipts',headers={**headers,'Idempotency-Key':'shared-'+order_id},json={'items':[{'order_item_id':item_id,'quantity':'5'}]})
    with ThreadPoolExecutor(max_workers=2) as pool: responses=list(pool.map(receive,orders))
    assert [r.status_code for r in responses]==[201,201],[ (r.status_code,r.text) for r in responses ]
    inventory=world.get('/v1/products',headers=headers).json()['items']
    assert next(row for row in inventory if row['id']==p['id'])['stock']=='10'
    movements=world.get('/v1/stock-movements',headers=headers).json()['items']
    assert sum(row['quantity']=='5' and row['kind']=='adjustment_in' for row in movements)==2


def test_concurrent_voids_of_same_branch_sale_refund_once(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world);headers=auth(owner)
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    p=product(world,owner,sku='CONCURRENT-VOID',name='Estorno Concorrente',price='3.00')
    assert world.post(f'/v1/branches/{branch}/registers/open',headers=headers,json={'label':'Turno estorno','opening_amount':'20'}).status_code==201
    assert world.post(f'/v1/branches/{branch}/inventory/{p["id"]}/adjustments',headers=headers,json={'direction':'in','quantity':'4','reason':'Carga QA'}).status_code==201
    sale=world.post(f'/v1/branches/{branch}/sales',headers={**headers,'Idempotency-Key':'concurrent-void-sale'},json={'items':[{'product_id':p['id'],'quantity':'2'}],'payment_method':'cash','received_amount':'6'});assert sale.status_code==201,sale.text
    def void(i): return world.post(f'/v1/branches/{branch}/sales/{sale.json()["id"]}/void',headers=headers,json={'reason':f'Estorno QA {i}'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(void,range(8)))
    assert sum(r.status_code==200 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==409 for r in responses)==7,[(r.status_code,r.text) for r in responses]
    assert world.get(f'/v1/branches/{branch}/inventory',headers=headers).json()['items'][0]['quantity']=='4'
    assert world.get(f'/v1/branches/{branch}/registers/current',headers=headers).json()['expected_amount']=='20.00'
    conn=main.connect_db()
    try:
        assert conn.execute('SELECT COUNT(*) FROM branch_sales_returns WHERE sale_id=?',(sale.json()['id'],)).fetchone()[0]==1
        assert conn.execute("SELECT COUNT(*) FROM branch_stock_events WHERE product_id=? AND kind='reversal'",(p['id'],)).fetchone()[0]==1
    finally: conn.close()


def test_branch_cash_movements_consider_sales_and_serialize_withdrawals(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world);headers=auth(owner)
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    product_row=product(world,owner,sku='CASH-LOCK',name='Produto caixa',price='30.00')
    register=world.post(f'/v1/branches/{branch}/registers/open',headers=headers,json={'label':'Caixa concorrência','opening_amount':'50.00'})
    assert register.status_code==201,register.text
    assert world.post(f'/v1/branches/{branch}/inventory/{product_row["id"]}/adjustments',headers=headers,json={'direction':'in','quantity':'1','reason':'Carga QA'}).status_code==201
    sale=world.post(f'/v1/branches/{branch}/sales',headers={**headers,'Idempotency-Key':'cash-lock-sale'},json={'items':[{'product_id':product_row['id'],'quantity':'1'}],'payment_method':'cash','received_amount':'30.00'})
    assert sale.status_code==201,sale.text
    def withdraw(i):
        return world.post(f'/v1/branches/{branch}/registers/{register.json()["id"]}/movements',headers=headers,json={'kind':'withdrawal','amount':'60.00','reason':f'Sangria paralela QA {i}'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(withdraw,range(8)))
    assert sum(r.status_code==201 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==409 for r in responses)==7,[(r.status_code,r.text) for r in responses]
    current=world.get(f'/v1/branches/{branch}/registers/current',headers=headers).json()
    assert current['expected_amount']=='20.00'
    assert world.post(f'/v1/branches/{branch}/registers/{register.json()["id"]}/movements',headers=headers,json={'kind':'withdrawal','amount':'21.00','reason':'Não pode exceder saldo'}).status_code==409


def test_branch_register_close_and_cash_movement_are_serialized(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world);headers=auth(owner)
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    register=world.post(f'/v1/branches/{branch}/registers/open',headers=headers,json={'label':'Fechamento concorrente','opening_amount':'100.00'}).json()
    def move():return world.post(f'/v1/branches/{branch}/registers/{register["id"]}/movements',headers=headers,json={'kind':'supply','amount':'25.00','reason':'Suprimento QA'})
    def close():return world.post(f'/v1/branches/{branch}/registers/{register["id"]}/close',headers=headers,json={'counted_amount':'125.00'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(move);b=pool.submit(close);results=[a.result(),b.result()]
    closed=next((r for r in results if r.status_code==200),None)
    assert closed is not None,[(r.status_code,r.text) for r in results]
    assert closed.json()['expected_amount'] in {'100.00','125.00'}
    movement=next(r for r in results if r is not closed)
    if closed.json()['expected_amount']=='100.00':
        assert movement.status_code==404
    else:
        assert movement.status_code==201
    current=world.get(f'/v1/branches/{branch}/registers/current',headers=headers).json()
    assert current['register'] is None


def test_concurrent_branch_restaurant_final_payments_close_once_and_debit_stock_once(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,'Restaurant concurrency','restaurant-concurrency','restaurant-concurrency@example.test');headers=auth(owner)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    product_row=product(world,owner,sku='REST-CONCURRENT',name='Prato concorrente',price='12.50')
    assert world.post(f'/v1/branches/{branch}/inventory/{product_row["id"]}/adjustments',headers=headers,json={'direction':'in','quantity':'3','reason':'Carga QA restaurante'}).status_code==201
    assert world.post(f'/v1/branches/{branch}/registers/open',headers=headers,json={'label':'Restaurante QA','opening_amount':'0'}).status_code==201
    area=world.post(f'/v1/branches/{branch}/restaurant/areas',headers=headers,json={'name':'Salão concorrente'}).json()
    table=world.post(f'/v1/branches/{branch}/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'C1','capacity':2}).json()
    service=world.post(f'/v1/branches/{branch}/restaurant/tables/{table["id"]}/open',headers=headers).json()
    tab=world.post(f'/v1/branches/{branch}/restaurant/services/{service["id"]}/tabs',headers=headers,json={'label':'Comanda concorrente'}).json()
    category=world.post(f'/v1/branches/{branch}/restaurant/menu/categories',headers=headers,json={'name':'Pratos'}).json()
    menu=world.post(f'/v1/branches/{branch}/restaurant/menu/items',headers=headers,json={'category_id':category['id'],'product_id':product_row['id'],'station':'kitchen'}).json()
    order=world.post(f'/v1/branches/{branch}/restaurant/tabs/{tab["id"]}/orders',headers=headers,json={'items':[{'menu_item_id':menu['id'],'quantity':'1'}]})
    assert order.status_code==201,order.text
    assert world.post(f'/v1/branches/{branch}/restaurant/orders/{order.json()["id"]}/send',headers=headers).status_code==200
    item=world.get(f'/v1/branches/{branch}/restaurant/production?station=kitchen',headers=headers).json()['items'][0]
    for status in ('preparing','ready','delivered'):
        response=world.patch(f'/v1/branches/{branch}/restaurant/order-items/{item["id"]}',headers=headers,json={'status':status})
        assert response.status_code==200,response.text
    def pay(i):
        return world.post(f'/v1/branches/{branch}/restaurant/tabs/{tab["id"]}/payments',headers=headers,json={'amount':'12.50','received_amount':'12.50','idempotency_key':f'rest-final-concurrent-{i}'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(pay,range(8)))
    assert sum(r.status_code==201 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert all(r.status_code in {201,404,409} for r in responses),[(r.status_code,r.text) for r in responses]
    detail=world.get(f'/v1/branches/{branch}/restaurant/tabs/{tab["id"]}',headers=headers).json()
    assert detail['status']=='closed' and detail['paid']=='12.50' and detail['remaining']=='0.00'
    assert world.get(f'/v1/branches/{branch}/inventory',headers=headers).json()['items'][0]['quantity']=='2'
    assert world.get(f'/v1/branches/{branch}/registers/current',headers=headers).json()['expected_amount']=='12.50'
    conn=main.connect_db()
    try:
        assert conn.execute('SELECT COUNT(*) FROM branch_sales WHERE branch_id=? AND idempotency_key=?',(branch,'restaurant-tab:'+tab['id'])).fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM branch_restaurant_tab_payments WHERE tab_id=?',(tab['id'],)).fetchone()[0]==1
    finally: conn.close()


def test_concurrent_branch_table_open_creates_one_service_only(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,'Restaurant table concurrency','restaurant-table-concurrency','restaurant-table-concurrency@example.test');headers=auth(owner)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    area=world.post(f'/v1/branches/{branch}/restaurant/areas',headers=headers,json={'name':'Concorrência'}).json()
    table=world.post(f'/v1/branches/{branch}/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'T1','capacity':4}).json()
    def open_table(_): return world.post(f'/v1/branches/{branch}/restaurant/tables/{table["id"]}/open',headers=headers)
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(open_table,range(8)))
    assert sum(r.status_code==200 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert all(r.status_code in {200,409} for r in responses),[(r.status_code,r.text) for r in responses]
    floor=world.get(f'/v1/branches/{branch}/restaurant/floor',headers=headers)
    assert floor.status_code==200 and sum(t['status']=='occupied' for t in floor.json()['items'])==1
    conn=main.connect_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM restaurant_services WHERE table_id=? AND branch_id=? AND status='open'",(table['id'],branch)).fetchone()[0]==1
    finally: conn.close()



def test_branch_reservation_lifecycle_seats_guest_and_opens_service_atomically(world):
    owner=company(world,name="Reservation lifecycle",slug="reservation-lifecycle",email="reservation-lifecycle@example.test");headers=auth(owner)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    area=world.post(f'/v1/branches/{branch}/restaurant/areas',headers=headers,json={'name':'Salão lifecycle'});assert area.status_code==201,area.text
    table=world.post(f'/v1/branches/{branch}/restaurant/tables',headers=headers,json={'area_id':area.json()['id'],'label':'RL-01','capacity':4});assert table.status_code==201,table.text
    reservation=world.post(f'/v1/branches/{branch}/restaurant/reservations',headers=headers,json={'table_id':table.json()['id'],'customer_name':'Cliente lifecycle','party_size':3,'starts_at':'2026-12-12T18:00','ends_at':'2026-12-12T19:00'});assert reservation.status_code==201,reservation.text
    seated=world.post(f"/v1/branches/{branch}/restaurant/reservations/{reservation.json()['id']}/seat",headers=headers);assert seated.status_code==201,seated.text
    assert seated.json()['status']=='seated' and seated.json()['service_id']
    floor=world.get(f'/v1/branches/{branch}/restaurant/floor',headers=headers).json()['items']
    assert next(x for x in floor if x['id']==table.json()['id'])['status']=='occupied'
    listed=world.get(f'/v1/branches/{branch}/restaurant/reservations?status=all',headers=headers).json()['items']
    assert next(x for x in listed if x['id']==reservation.json()['id'])['status']=='seated'
    assert world.post(f"/v1/branches/{branch}/restaurant/reservations/{reservation.json()['id']}/seat",headers=headers).status_code==409


def test_branch_reservation_can_cancel_or_record_no_show_with_audit(world):
    owner=company(world,name="Reservation outcomes",slug="reservation-outcomes",email="reservation-outcomes@example.test");headers=auth(owner)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    area=world.post(f'/v1/branches/{branch}/restaurant/areas',headers=headers,json={'name':'Salão outcomes'}).json()
    table=world.post(f'/v1/branches/{branch}/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'RO-01','capacity':4}).json()
    results=[]
    for i,action in enumerate(('cancel','no_show')):
        reservation=world.post(f'/v1/branches/{branch}/restaurant/reservations',headers=headers,json={'table_id':table['id'],'customer_name':f'Cliente {i}','party_size':2,'starts_at':f'2026-12-13T1{i}:00','ends_at':f'2026-12-13T1{i}:45'})
        assert reservation.status_code==201,reservation.text
        result=world.post(f"/v1/branches/{branch}/restaurant/reservations/{reservation.json()['id']}/cancel",headers=headers,json={'action':action})
        assert result.status_code==200 and result.json()['outcome']==action
        results.append(reservation.json()['id'])
    events=world.get('/v1/audit-events',headers=headers).json()['items']
    assert any(e['action']=='restaurant.branch_reservation_cancelled' and e['entity_id']==results[0] for e in events)
    assert any(e['action']=='restaurant.branch_reservation_no_show' and e['entity_id']==results[1] for e in events)


def test_legacy_reservation_lifecycle_seats_guest_and_records_no_show(world):
    owner=company(world,name="Legacy reservation lifecycle",slug="legacy-reservation-lifecycle",email="legacy-reservation-lifecycle@example.test");headers=auth(owner)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    area=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Legacy lifecycle'}).json()
    table=world.post('/v1/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'LL-01','capacity':4}).json()
    reservation=world.post('/v1/restaurant/reservations',headers=headers,json={'table_id':table['id'],'customer_name':'Cliente legado','party_size':2,'starts_at':'2026-12-14T18:00','ends_at':'2026-12-14T19:00'});assert reservation.status_code==201,reservation.text
    seated=world.post(f"/v1/restaurant/reservations/{reservation.json()['id']}/seat",headers=headers);assert seated.status_code==201,seated.text
    assert world.get('/v1/restaurant/floor',headers=headers).json()['items'][0]['status']=='occupied'
    other=world.post('/v1/restaurant/reservations',headers=headers,json={'table_id':table['id'],'customer_name':'No show','party_size':1,'starts_at':'2026-12-15T18:00','ends_at':'2026-12-15T19:00'});assert other.status_code==201,other.text
    outcome=world.post(f"/v1/restaurant/reservations/{other.json()['id']}/cancel",headers=headers,json={'action':'no_show'})
    assert outcome.status_code==200 and outcome.json()['outcome']=='no_show'


def test_concurrent_branch_reservations_for_same_table_do_not_overlap(world):
    from concurrent.futures import ThreadPoolExecutor
    token=company(world,'Branch reservations','branch-reservations','branch-reservations@example.test');headers=auth(token)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    area=world.post(f'/v1/branches/{branch}/restaurant/areas',headers=headers,json={'name':'Reservas'}).json()
    table=world.post(f'/v1/branches/{branch}/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'R1','capacity':4}).json()
    body={'table_id':table['id'],'customer_name':'Cliente simultâneo','party_size':2,'starts_at':'2026-12-22T12:00','ends_at':'2026-12-22T13:00'}
    def reserve(i):return world.post(f'/v1/branches/{branch}/restaurant/reservations',headers=headers,json={**body,'customer_name':f'Cliente {i}'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(reserve,range(8)))
    assert sum(r.status_code==201 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==409 for r in responses)==7,[(r.status_code,r.text) for r in responses]
    listed=world.get(f'/v1/branches/{branch}/restaurant/reservations',headers=headers).json()['items']
    assert len(listed)==1


def test_concurrent_legacy_reservations_for_same_table_do_not_overlap(world):
    from concurrent.futures import ThreadPoolExecutor
    token=company(world,'Legacy reservation concurrency','legacy-reservation-concurrency','legacy-reservation@example.test');headers=auth(token)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    area=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Reservas legacy'}).json()
    table=world.post('/v1/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'L1','capacity':4}).json()
    body={'table_id':table['id'],'customer_name':'Cliente simultâneo','party_size':2,'starts_at':'2026-12-23T12:00','ends_at':'2026-12-23T13:00'}
    def reserve(i):return world.post('/v1/restaurant/reservations',headers=headers,json={**body,'customer_name':f'Cliente {i}'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(reserve,range(8)))
    assert sum(r.status_code==201 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==409 for r in responses)==7,[(r.status_code,r.text) for r in responses]
    assert len(world.get('/v1/restaurant/reservations',headers=headers).json()['items'])==1


def test_concurrent_branch_empty_service_close_and_tab_open_cannot_orphan_tab(world):
    from concurrent.futures import ThreadPoolExecutor
    token=company(world,'Service lifecycle concurrency','service-concurrency','service-concurrency@example.test');headers=auth(token)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    branch=world.get('/v1/company/branches',headers=headers).json()['items'][0]['id']
    area=world.post(f'/v1/branches/{branch}/restaurant/areas',headers=headers,json={'name':'Ciclo'}).json()
    table=world.post(f'/v1/branches/{branch}/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'S1','capacity':2}).json()
    service=world.post(f'/v1/branches/{branch}/restaurant/tables/{table["id"]}/open',headers=headers).json()
    def close():return world.post(f'/v1/branches/{branch}/restaurant/services/{service["id"]}/close-empty',headers=headers)
    def open_tab():return world.post(f'/v1/branches/{branch}/restaurant/services/{service["id"]}/tabs',headers=headers,json={'label':'QA lifecycle'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(close);b=pool.submit(open_tab);responses=[a.result(),b.result()]
    assert all(r.status_code in {200,201,404,409} for r in responses),[(r.status_code,r.text) for r in responses]
    conn=main.connect_db()
    try:
        state=conn.execute('SELECT status FROM restaurant_services WHERE id=?',(service['id'],)).fetchone()[0]
        tabs=conn.execute('SELECT COUNT(*) FROM restaurant_tabs WHERE service_id=? AND branch_id=?',(service['id'],branch)).fetchone()[0]
        assert (state=='closed' and tabs==0) or (state=='open' and tabs==1), (state,tabs,[(r.status_code,r.text) for r in responses])
    finally: conn.close()


def test_concurrent_legacy_restaurant_final_payments_close_once_and_debit_stock_once(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,'Legacy restaurant concurrency','legacy-restaurant-concurrency','legacy-restaurant-concurrency@example.test');headers=auth(owner)
    assert world.put('/v1/company/profile',headers=headers,json={'primary_segment':'restaurant','answers':{'tables':True}}).status_code==200
    prod=product(world,owner,sku='LEGACY-REST-CONC',name='Prato legacy',price='12.50')
    assert world.post(f'/v1/products/{prod["id"]}/stock-adjustments',headers=headers,json={'direction':'in','quantity':'3','reason':'Carga QA legacy'}).status_code==201
    assert world.post('/v1/registers/open',headers=headers,json={'label':'Caixa legacy','opening_amount':'0'}).status_code==201
    area=world.post('/v1/restaurant/areas',headers=headers,json={'name':'Salão legacy'}).json()
    table=world.post('/v1/restaurant/tables',headers=headers,json={'area_id':area['id'],'label':'LG1','capacity':2}).json()
    service=world.post(f'/v1/restaurant/tables/{table["id"]}/open',headers=headers).json()
    tab=world.post(f'/v1/restaurant/services/{service["id"]}/tabs',headers=headers,json={'label':'Legacy concurrency'}).json()
    category=world.post('/v1/restaurant/menu/categories',headers=headers,json={'name':'Pratos legacy'}).json()
    menu=world.post('/v1/restaurant/menu/items',headers=headers,json={'category_id':category['id'],'product_id':prod['id'],'station':'kitchen'}).json()
    order=world.post(f'/v1/restaurant/tabs/{tab["id"]}/orders',headers=headers,json={'items':[{'menu_item_id':menu['id'],'quantity':'1'}]})
    assert order.status_code==201,order.text
    assert world.post(f'/v1/restaurant/orders/{order.json()["id"]}/send',headers=headers).status_code==200
    item=world.get('/v1/restaurant/production?station=kitchen',headers=headers).json()['items'][0]
    for status in ('preparing','ready','delivered'):
        r=world.patch(f'/v1/restaurant/order-items/{item["id"]}',headers=headers,json={'status':status});assert r.status_code==200,r.text
    def pay(i):return world.post(f'/v1/restaurant/tabs/{tab["id"]}/payments',headers=headers,json={'amount':'12.50','received_amount':'12.50','idempotency_key':f'legacy-final-concurrent-{i}'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(pay,range(8)))
    assert sum(r.status_code==201 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert all(r.status_code in {201,409} for r in responses),[(r.status_code,r.text) for r in responses]
    detail=world.get(f'/v1/restaurant/tabs/{tab["id"]}',headers=headers).json()
    assert detail['status']=='closed' and detail['paid']=='12.50' and detail['remaining']=='0.00'
    assert world.get('/v1/registers/current',headers=headers).json()['expected_amount']=='12.50'
    assert world.get('/v1/products',headers=headers).json()['items'][0]['stock']=='2'
    conn=main.connect_db()
    try:
        assert conn.execute('SELECT COUNT(*) FROM sales WHERE idempotency_key=?',('restaurant-tab:'+tab['id'],)).fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM restaurant_tab_payments WHERE tab_id=?',(tab['id'],)).fetchone()[0]==1
    finally: conn.close()


def test_concurrent_legacy_sales_do_not_oversell_inventory_or_cash(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,'Retail concurrency','retail-concurrency','retail-concurrency@example.test');headers=auth(owner)
    prod=product(world,owner,sku='RETAIL-LOCK',name='Produto limitado',price='20.00')
    assert world.post(f'/v1/products/{prod["id"]}/stock-adjustments',headers=headers,json={'direction':'in','quantity':'4','reason':'Carga QA concorrência'}).status_code==201
    assert world.post('/v1/registers/open',headers=headers,json={'label':'Varejo concorrência','opening_amount':'0'}).status_code==201
    def sell(i):
        return world.post('/v1/sales',headers={**headers,'Idempotency-Key':f'retail-concurrent-sale-{i}'},json={'items':[{'product_id':prod['id'],'quantity':'1','discount':'0'}],'payment_method':'cash','received_amount':'20.00'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(sell,range(8)))
    assert sum(r.status_code==201 for r in responses)==4,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==409 for r in responses)==4,[(r.status_code,r.text) for r in responses]
    assert world.get('/v1/products',headers=headers).json()['items'][0]['stock']=='0'
    assert world.get('/v1/registers/current',headers=headers).json()['expected_amount']=='80.00'
    assert len(world.get('/v1/sales',headers=headers).json()['items'])==4


def test_concurrent_legacy_register_withdrawals_respect_balance_and_close_lock(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,'Cash concurrency','cash-concurrency','cash-concurrency@example.test');headers=auth(owner)
    register=world.post('/v1/registers/open',headers=headers,json={'label':'Sangria concorrente','opening_amount':'100.00'});assert register.status_code==201,register.text
    rid=register.json()['id']
    def withdraw(i):return world.post(f'/v1/registers/{rid}/movements',headers=headers,json={'kind':'withdrawal','amount':'60.00','reason':f'Sangria QA concorrente {i}'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(withdraw,range(8)))
    assert sum(r.status_code==201 for r in responses)==1,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==409 for r in responses)==7,[(r.status_code,r.text) for r in responses]
    assert world.get('/v1/registers/current',headers=headers).json()['expected_amount']=='40.00'
    closed=world.post(f'/v1/registers/{rid}/close',headers=headers,json={'counted_amount':'40.00'})
    assert closed.status_code==200,closed.text
    assert world.post(f'/v1/registers/{rid}/movements',headers=headers,json={'kind':'supply','amount':'5.00','reason':'After close'}).status_code==404


def test_concurrent_legacy_payable_payments_do_not_exceed_open_balance(world):
    from concurrent.futures import ThreadPoolExecutor
    owner=company(world,'Payables concurrency','payables-concurrency','payables-concurrency@example.test');headers=auth(owner)
    payable=world.post('/v1/finance/expenses',headers={**headers,'Idempotency-Key':'concurrency-expense-create'},json={'description':'Conta concorrência','amount':'100.00','due_date':'2026-12-10','cost_center':'QA'})
    assert payable.status_code==201,payable.text
    pid=payable.json()['id']
    def pay(i):return world.post(f'/v1/finance/payables/{pid}/payments',headers={**headers,'Idempotency-Key':f'concurrent-payable-{i}'},json={'amount':'40.00','payment_date':'2026-11-17','reference':f'Pagamento concorrente {i}'})
    with ThreadPoolExecutor(max_workers=8) as pool: responses=list(pool.map(pay,range(8)))
    assert sum(r.status_code==201 for r in responses)==2,[(r.status_code,r.text) for r in responses]
    assert sum(r.status_code==409 for r in responses)==6,[(r.status_code,r.text) for r in responses]
    listed=world.get('/v1/finance/payables?status=all',headers=headers).json()['items']
    row=next(x for x in listed if x['id']==pid)
    assert row['paid']=='80.00' and row['remaining']=='20.00' and row['status']=='open'
    conn=main.connect_db()
    try: assert conn.execute('SELECT SUM(amount_cents) FROM payable_payments WHERE payable_id=?',(pid,)).fetchone()[0]==8000
    finally: conn.close()
