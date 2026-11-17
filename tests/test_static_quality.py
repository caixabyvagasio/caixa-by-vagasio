from pathlib import Path
import warnings


def test_python_backend_compiles_without_syntax_warnings():
    source = Path(__file__).parents[1] / "app" / "main.py"
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always", SyntaxWarning)
        compile(source.read_text(), str(source), "exec")
    assert not [w for w in seen if issubclass(w.category, SyntaxWarning)]


def test_backend_catalog_has_unique_feature_status_keys():
    import ast
    source = (Path(__file__).parents[1] / "app" / "main.py").read_text()
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MODULE_STATUSES" for t in n.targets))
    keys = [ast.literal_eval(k) for k in node.value.keys]
    assert len(keys) == len(set(keys))
