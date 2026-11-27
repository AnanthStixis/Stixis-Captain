"""Adopters, preset downloads and install guides (plan step 4)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from captain.api.admin_adopters import install_adopter_routes
from captain.api.admin_queries import InMemoryAdminQueries
from captain.api.adopters import CATALOG, REPO_ROOT, Urls
from captain.core.admin import AdminActor
from captain.core.identity.rbac import PermissionDenied
from tests.harness.adapters import CaptainCoreHarness

GW = "https://captain.abc.example"


def fake_artifacts(root: Path) -> Path:
    (root / "ide" / "vscode").mkdir(parents=True)
    with zipfile.ZipFile(root / "ide" / "vscode" / "captain-vscode-0.1.0.vsix", "w") as z:
        pkg = {
            "contributes": {
                "configuration": {
                    "properties": {
                        "captain.gatewayUrl": {
                            "type": "string",
                            "default": "http://127.0.0.1:8000",
                        },
                        "captain.allowPlainHttp": {"type": "boolean", "default": False},
                    }
                }
            }
        }
        z.writestr("extension/package.json", json.dumps(pkg))
        z.writestr("extension/dist/extension.js", "// code")  # fmt: skip
    dist = root / "browser" / "extension" / "dist"
    dist.mkdir(parents=True)
    (dist / "manifest.json").write_text(json.dumps(
        {"name": "Stixis Captain", "host_permissions": ["http://127.0.0.1:8000/*"]}))  # fmt: skip
    (dist / "background.js").write_text("// sw")
    desk = root / "desktop" / "windows"
    desk.mkdir(parents=True)
    for f in ("Invoke-CaptainDesktopAgent.ps1", "Register-CaptainDesktopAgent.ps1"):
        (desk / f).write_text("# ps")
    (root / "ide" / "claude-code").mkdir(parents=True)
    (root / "ide" / "claude-code" / "captain_hook.py").write_text("# hook")
    return root


def rig(
    tmp_path: Path, roles: frozenset[str] = frozenset({"org-admin"})
) -> tuple[CaptainCoreHarness, TestClient]:  # noqa: E501
    h = CaptainCoreHarness()
    app = FastAPI()

    @app.exception_handler(PermissionDenied)
    async def _denied(_: Any, e: PermissionDenied) -> JSONResponse:
        return JSONResponse({"error": "FORBIDDEN"}, status_code=403)

    env = {
        "CAPTAIN_PUBLIC_GATEWAY_URL": GW + "/",
        "CAPTAIN_PUBLIC_MCP_URL": GW + "/mcp",
        "CAPTAIN_PUBLIC_CORE_URL": "https://core.abc.example",
        "CAPTAIN_ARTIFACTS_DIR": str(fake_artifacts(tmp_path / "art")),
        "CAPTAIN_GUIDES_DIR": str(REPO_ROOT / "docs" / "guides"),
    }
    install_adopter_routes(app, InMemoryAdminQueries(h.built.infra),
                           lambda: AdminActor("priya@abc.example", roles), env)  # fmt: skip
    return h, TestClient(app)


def unzip(data: bytes) -> dict[str, bytes]:
    z = zipfile.ZipFile(io.BytesIO(data))
    return {n: z.read(n) for n in z.namelist()}


def test_catalog_lists_every_adopter_with_its_guide(tmp_path: Path) -> None:
    _, c = rig(tmp_path)
    body = c.get("/admin/v1/adopters").json()
    assert body["urls"]["gateway"] == GW  # trailing slash removed
    ids = [a["id"] for a in body["items"]]
    assert {"vscode", "claude-code", "browser", "mcp", "sdk-python", "api", "desktop-agent"} <= set(
        ids
    )
    for a in CATALOG:  # every adopter has a guide file in the repository
        assert (REPO_ROOT / "docs" / "guides" / f"{a.guide}.md").exists(), a.guide


def test_vscode_package_defaults_to_this_install(tmp_path: Path) -> None:
    _, c = rig(tmp_path)
    r = c.get("/admin/v1/downloads/vscode-extension")
    assert (
        r.status_code == 200
        and 'filename="captain-vscode.vsix"' in r.headers["content-disposition"]
    )
    files = unzip(r.content)
    pkg = json.loads(files["extension/package.json"])
    assert pkg["contributes"]["configuration"]["properties"]["captain.gatewayUrl"]["default"] == GW
    assert files["extension/dist/extension.js"] == b"// code"  # the rest is unchanged


def test_browser_package_may_only_talk_to_this_install(tmp_path: Path) -> None:
    _, c = rig(tmp_path)
    files = unzip(c.get("/admin/v1/downloads/browser-extension").content)
    assert json.loads(files["manifest.json"])["host_permissions"] == [GW + "/*"]
    assert json.loads(files["preset.json"]) == {"captainUrl": GW}


def test_desktop_and_claude_code_packages_carry_the_address(tmp_path: Path) -> None:
    _, c = rig(tmp_path)
    desk = unzip(c.get("/admin/v1/downloads/desktop-agent").content)
    assert json.loads(desk["captain.json"]) == {"captainUrl": GW}
    cc = unzip(c.get("/admin/v1/downloads/claude-code-hook").content)
    assert json.loads(cc["captain-settings-snippet.json"])["env"]["CAPTAIN_CORE_URL"] == (
        "https://core.abc.example"
    )


@pytest.mark.parametrize(
    "download_id",
    ["vscode-copilot-model", "mcp-vscode", "mcp-claude-desktop", "mcp-cursor", "browser-policy"],
)
def test_config_files_use_this_install_and_hold_no_key(tmp_path: Path, download_id: str) -> None:
    _, c = rig(tmp_path)
    r = c.get(f"/admin/v1/downloads/{download_id}")
    assert r.status_code == 200
    text = r.text
    assert GW in text
    assert "cpt_" not in text  # never a real key: placeholders or ${env:...} only


def test_missing_artifacts_and_unknown_downloads(tmp_path: Path) -> None:
    h = CaptainCoreHarness()
    app = FastAPI()
    install_adopter_routes(app, InMemoryAdminQueries(h.built.infra),
                           lambda: AdminActor("p", frozenset({"org-admin"})),
                           {"CAPTAIN_ARTIFACTS_DIR": str(tmp_path)})  # fmt: skip
    c = TestClient(app)
    r = c.get("/admin/v1/downloads/vscode-extension")
    assert r.status_code == 503 and "NOT_BUILT" in r.text
    assert c.get("/admin/v1/downloads/../../etc/passwd").status_code == 404
    assert c.get("/admin/v1/downloads/nope").status_code == 404


def test_guides_have_this_install_filled_in_and_refuse_paths(tmp_path: Path) -> None:
    _, c = rig(tmp_path)
    g = c.get("/admin/v1/guides/vscode")
    assert g.status_code == 200 and "{{GATEWAY_URL}}" not in g.text and GW in g.text
    assert c.get("/admin/v1/guides/..%2Fsecret").status_code == 404
    assert c.get("/admin/v1/guides/nope").status_code == 404


def test_every_guide_follows_the_same_shape() -> None:
    for a in CATALOG:
        text = (REPO_ROOT / "docs" / "guides" / f"{a.guide}.md").read_text(encoding="utf-8")
        assert text.startswith("# "), a.guide
        assert "## Test it" in text and "Expected" in text, a.guide


def test_viewers_without_register_read_are_refused(tmp_path: Path) -> None:
    _, c = rig(tmp_path, frozenset())
    assert c.get("/admin/v1/adopters").status_code == 403
    assert c.get("/admin/v1/downloads/mcp-vscode").status_code == 403


def test_urls_fill() -> None:
    u = Urls(gateway="g", mcp="m", core="c", console="x")
    assert u.fill("{{GATEWAY_URL}} {{MCP_URL}} {{CORE_URL}} {{CONSOLE_URL}}") == "g m c x"


def test_plain_http_is_allowed_only_in_packages_from_a_plain_http_install(tmp_path: Path) -> None:
    from captain.api.adopters import Urls, vscode_vsix

    art = fake_artifacts(tmp_path)

    def allowed(gateway: str) -> bool:
        pkg = json.loads(unzip(vscode_vsix(art, Urls(gateway=gateway)))["extension/package.json"])
        return bool(
            pkg["contributes"]["configuration"]["properties"]["captain.allowPlainHttp"]["default"]
        )

    assert allowed("http://glixify.10.0.0.16.nip.io") is True  # QA on a closed network
    assert allowed("https://abc.stixis.com") is False
    assert allowed("http://127.0.0.1:8000") is False  # this machine needs no exception


def test_browser_package_allows_plain_http_only_from_a_plain_http_install(tmp_path: Path) -> None:
    from captain.api.adopters import Urls, browser_zip

    art = fake_artifacts(tmp_path)

    def preset(gateway: str) -> dict[str, object]:
        return dict(json.loads(unzip(browser_zip(art, Urls(gateway=gateway)))["preset.json"]))

    assert preset("http://glixify.10.0.0.16.nip.io") == {
        "captainUrl": "http://glixify.10.0.0.16.nip.io", "allowPlainHttp": True}  # fmt: skip
    assert "allowPlainHttp" not in preset("https://abc.stixis.com")
