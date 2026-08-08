from pathlib import Path

from tools.brew_resources import render_resources

LOCK = Path(__file__).resolve().parent.parent / "uv.lock"


def test_includes_runtime_and_standard_extra_deps():
    out = render_resources(LOCK)
    # Direct runtime deps
    assert 'resource "fastapi" do' in out
    assert 'resource "jinja2" do' in out
    assert 'resource "uvicorn" do' in out
    # uvicorn[standard] transitives must be present (the whole point)
    for pkg in ("httptools", "python-dotenv", "pyyaml", "uvloop", "watchfiles", "websockets"):
        assert f'resource "{pkg}" do' in out, f"missing standard-extra resource {pkg}"
    # Deeper transitives
    for pkg in ("pydantic", "pydantic-core", "starlette", "anyio", "markupsafe", "click", "h11"):
        assert f'resource "{pkg}" do' in out, f"missing transitive resource {pkg}"


def test_excludes_dev_only_deps():
    out = render_resources(LOCK)
    for pkg in ("pytest", "httpx2", "httpcore2", "iniconfig", "pluggy"):
        assert f'resource "{pkg}" do' not in out, f"dev-only {pkg} leaked into resources"
    assert 'resource "bridge" do' not in out


def test_each_resource_has_pypi_url_and_sha256():
    out = render_resources(LOCK)
    blocks = out.count(" do")
    assert out.count("url \"https://files.pythonhosted.org/") == blocks
    assert out.count("sha256 \"") == blocks
    # sha256 is a bare 64-hex, not the lockfile's "sha256:" prefix
    assert "sha256 \"sha256:" not in out
