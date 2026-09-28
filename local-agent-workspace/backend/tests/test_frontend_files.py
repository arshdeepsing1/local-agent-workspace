import re

from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import APP_ROOT, Settings


def test_frontend_source_is_served_directly_without_a_build(tmp_path):
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    with TestClient(create_app(settings)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-cache"
        # Every file the page and its modules load must be served as the right type.
        paths = re.findall(r'"(/static/[^"]+)"', page.text)
        assert "/static/js/main.js" in paths and "/static/vendor/preact.module.js" in paths
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["cache-control"] == "no-cache"
            if path.endswith(".js"):
                assert response.headers["content-type"].startswith("text/javascript"), path


def test_every_module_import_resolves_to_a_frontend_file():
    static = APP_ROOT / "frontend" / "static"
    bare = {"preact", "preact/hooks", "htm"}
    for module in (static / "js").rglob("*.js"):
        for target in re.findall(r"""^import .*?from '([^']+)'""", module.read_text(), re.M):
            if target in bare:
                continue
            assert (module.parent / target).resolve().is_file(), f"{module.relative_to(static)} imports missing {target}"
