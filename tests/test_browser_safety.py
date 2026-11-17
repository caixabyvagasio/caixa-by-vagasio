from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
HTML=(ROOT/"frontend/index.html").read_text()
JS=(ROOT/"frontend/app.js").read_text()
CSS=(ROOT/"frontend/app.css").read_text()

def test_frontend_references_exist_and_forms_are_balanced():
    ids=set(re.findall(r'id="([^"]+)"',HTML))
    refs=set(re.findall(r"\$\('#([^']+)'\)",JS))
    assert refs-ids==set()
    assert HTML.count('<form')==HTML.count('</form>')
    assert len(ids)==len(re.findall(r'id="([^"]+)"',HTML))

def test_sensitive_client_actions_are_not_rendered_unconditionally():
    source=JS[JS.index("async function loadCustomers"):JS.index("async function showCustomerHistory")]
    assert "canReadPII" in source
    assert "canReadPII?`<button" in source
    assert "customer-private-field" in source

def test_mobile_breakpoints_and_security_headers_present():
    assert '@media(max-width:980px)' in CSS
    assert '@media(max-width:620px)' in CSS
    app=(ROOT/"app/main.py").read_text()
    for header in ['X-Content-Type-Options','X-Frame-Options','Content-Security-Policy','Cache-Control']:
        assert header in app


def test_public_environment_bootstrap_is_async_before_await():
    assert "async function loadPublicEnvironment(){try{const r=await fetch('/health'" in JS



def test_public_landing_matches_caixa_brand_and_avoids_unimplemented_claims():
    for text in ['landing-header','landing-layout','landing-copy','preview-shell','PRÉVIA ILUSTRATIVA','Varejo geral','Supermercados e mercearias','Restaurantes e lanchonetes','Lojas de roupas e calçados','Materiais de construção','Prestadores de serviços']:
        assert text in HTML or text in CSS
    assert 'Valores demonstrativos não são dados reais.' in HTML
    assert 'As ferramentas específicas de cada segmento são liberadas conforme sua implementação e validação.' in HTML
    assert 'Integrações completas' not in HTML


def test_user_supplied_logo_assets_are_referenced_and_present():
    from pathlib import Path
    for asset in ['caixa-logo-branca-rosa.png','caixa-logo-vinho-rosa.png','caixa-simbolo-branco-rosa.png','favicon.png']:
        assert asset in HTML or asset in CSS
        assert (ROOT/'frontend'/'assets'/asset).is_file() or (ROOT/'frontend'/asset).is_file()
    assert 'alt="Caixa by VAGASIO"' in HTML
