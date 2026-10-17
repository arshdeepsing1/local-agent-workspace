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


def test_the_web_app_manifest_and_its_icons_are_served(tmp_path):
    # Install app / Add to Dock (M8g1): a standalone window from the manifest, with PNG icons of the sizes it names.
    import json
    import struct
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    with TestClient(create_app(settings)) as client:
        page = client.get("/").text
        assert '<link rel="manifest" href="/static/manifest.webmanifest" />' in page
        assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png" />' in page
        response = client.get("/static/manifest.webmanifest")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/manifest+json")
        assert response.headers["cache-control"] == "no-cache"
        manifest = json.loads(response.text)
        assert manifest["display"] == "standalone" and manifest["start_url"] == manifest["scope"] == "/"
        assert manifest["name"] and manifest["short_name"] and manifest["theme_color"] == manifest["background_color"]
        assert "serviceworker" not in response.text.lower()  # Nothing caches the app's responses.
        for icon in [*manifest["icons"], {"src": "/static/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"}]:
            served = client.get(icon["src"])
            assert served.status_code == 200 and served.headers["content-type"] == icon["type"], icon
            if icon["type"] == "image/png":
                assert served.content[:8] == b"\x89PNG\r\n\x1a\n"
                width, height = struct.unpack(">II", served.content[16:24])
                assert f"{width}x{height}" == icon["sizes"], icon
            else:
                assert icon["sizes"] == "any" and served.text.startswith("<svg ")
