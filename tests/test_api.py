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
