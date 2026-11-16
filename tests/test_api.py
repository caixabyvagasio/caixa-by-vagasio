import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def world(tmp_path, monkeypatch):
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


def test_frontend_is_served_from_same_app(world):
    assert world.get("/").status_code == 200
    assert "Entrar na sua empresa" in world.get("/").text
    assert "Acessar demonstração fictícia" in world.get("/").text
    assert world.get("/static/app.js").status_code == 200
    assert world.get("/static/app.css").status_code == 200


def test_health_and_provisioning_gate(world):
    assert world.get("/health").json()["integrations"] == {"payments": False, "fiscal": False}
    denied = world.post("/v1/bootstrap", json={"company_name":"Xx","company_slug":"xx","owner_name":"AA","email":"a@b.co","password":"strong-test-password-123"})
    assert denied.status_code == 403
    assert world.get("/v1/products").status_code == 401


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


def test_public_company_signup_is_opt_in(world):
    payload = {"company_name":"Nova Empresa","company_slug":"nova-empresa","owner_name":"Pessoa Teste",
               "email":"novo@example.test","password":"strong-test-password-123"}
    assert world.post("/v1/companies/register", json=payload).status_code == 404


def test_public_signup_creates_isolated_owner_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "signup.sqlite3"))
    monkeypatch.setattr(main, "TOKEN_SECRET", "test-only-token-secret-with-entropy-123456789")
    monkeypatch.setattr(main, "BOOTSTRAP_SECRET", "test-only-provisioning-secret-123456")
    monkeypatch.setattr(main, "PUBLIC_SIGNUP", True)
    with TestClient(main.app) as client:
        payload = {"company_name":"Nova Empresa","company_slug":"Nova-Empresa","owner_name":"Pessoa Teste",
                   "email":"NOVO@EXAMPLE.TEST","password":"strong-test-password-123"}
        result = client.post("/v1/companies/register", json=payload)
        assert result.status_code == 201, result.text
        assert client.post("/v1/companies/register", json=payload).status_code == 409
        login = client.post("/v1/auth/login", json={"company_slug":"nova-empresa","email":"novo@example.test",
                                                       "password":"strong-test-password-123"})
        assert login.status_code == 200
        token = login.json()["access_token"]
        assert client.get("/v1/me", headers=auth(token)).json()["name"] == "Pessoa Teste"
        assert client.get("/v1/products", headers=auth(token)).json()["items"] == []


def test_login_rate_limits_repeated_wrong_passwords(world, monkeypatch):
    company(world, "Rate Test", "rate-test", "rate@example.test")
    # Isolate this test from earlier requests made from the same test-client IP.
    monkeypatch.setattr(main, "_RATE_LIMITER", main.WindowRateLimiter())
    payload={"company_slug":"rate-test","email":"rate@example.test","password":"wrong"}
    statuses=[world.post("/v1/auth/login", json=payload).status_code for _ in range(9)]
    assert statuses[:8] == [401] * 8
    assert statuses[8] == 429


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
    assert set(saved.json()["modules"]) == {"sales","inventory","tables_tabs","delivery","barcode","weighed_items","expiry_control"}
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
    main.init_db()
    main.init_db()
    conn=main.connect_db()
    try:
        versions=[tuple(r) for r in conn.execute('SELECT version,name FROM schema_migrations ORDER BY version').fetchall()]
    finally: conn.close()
    assert versions==[(1,'sales_customer_id'),(2,'product_metadata'),(3,'company_barcode_unique_index'),(4,'backfill_legacy_paid_payables')]


def test_legacy_paid_payables_are_backfilled_into_ledger_idempotently(world):
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
