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

