"""预演补丁脚本：snapshot → verify → apply 的闭环，以及版本不符时的拒绝语义。

全部用 tmp_path 造「上游 + 我们的改动」，不碰真实 vendored 仓库（那是 gitignore 的运行产物，
在别的机器上可能根本不存在，测试依赖它就等于依赖开发机的目录状态）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "previs_patch.py"
VERSION = "1.2.3"
UPSTREAM_FILES = {
    "src/config.ts": "export const label = '编辑戏段';\n",
    "src/engine.ts": "export class Engine {}\n",
}
OUR_CHANGE = "src/readability.ts"
OUR_PATCHED = "export const readability = true;\n"
OUR_TWEAK = "src/config.ts"
OUR_TWEAKED = "export const label = '编辑戏段';\nexport const commitLabel = '接口批量修改';\n"


@pytest.fixture(scope="module")
def previs():
    spec = importlib.util.spec_from_file_location("previs_patch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_repo(root: Path, *, files: dict[str, str], version: str = VERSION) -> Path:
    """造一份 vendored 仓库：package.json 带 version/shortVersion，其余按 files 写。"""
    root.mkdir(parents=True, exist_ok=True)
    package = {"name": "director-desk", "version": version, "shortVersion": version}
    (root / "package.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def patched_repo(tmp_path):
    """上游原样 + 我们改过的仓库（snapshot 的来源）。"""
    files = dict(UPSTREAM_FILES)
    files[OUR_CHANGE] = OUR_PATCHED
    files[OUR_TWEAK] = OUR_TWEAKED
    return write_repo(tmp_path / "vendored", files=files)


@pytest.fixture
def clean_repo(tmp_path):
    """干净副本：只有上游原样，缺我们的新文件，也缺那处改动。"""
    return write_repo(tmp_path / "clean", files=dict(UPSTREAM_FILES))


def snapshot(previs, repo: Path, out: Path, *extra: str) -> int:
    argv = ["snapshot", "--repo", str(repo), "--out", str(out), "--files", *extra]
    return previs.main(argv)


@pytest.fixture
def patch_dir(previs, patched_repo, tmp_path):
    out = tmp_path / "patches" / VERSION
    assert snapshot(previs, patched_repo, out, OUR_CHANGE, OUR_TWEAK) == 0
    return out


def test_snapshot_writes_manifest_and_full_contents(previs, patched_repo, patch_dir):
    """snapshot 导出完整文件内容 + 可核对的 manifest（版本、每文件 sha256、来源说明）。"""
    manifest = json.loads((patch_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["upstream_version"] == VERSION
    assert manifest["schema"] == 1
    assert manifest["summary"]
    listed = {entry["path"]: entry for entry in manifest["files"]}
    assert set(listed) == {OUR_CHANGE, OUR_TWEAK}
    # 内容必须是逐字节的完整文件，而不是 diff 片段
    assert (patch_dir / "files" / OUR_CHANGE).read_text(encoding="utf-8") == OUR_PATCHED
    assert (patch_dir / "files" / OUR_TWEAK).read_text(encoding="utf-8") == OUR_TWEAKED
    for relative, entry in listed.items():
        assert entry["sha256"] == previs.sha256_bytes(
            (patch_dir / "files" / relative).read_bytes()
        )
    # 默认没给 --force 时不许悄悄覆盖已有补丁
    assert snapshot(previs, patched_repo, patch_dir, OUR_CHANGE) == previs.EXIT_USAGE


def test_verify_clean_then_reports_drift(previs, patched_repo, patch_dir):
    """verify：一致时退出 0；改一个字节或缺一个文件就非零并点名。"""
    assert previs.main(["verify", "--repo", str(patched_repo), "--patch", str(patch_dir)]) == 0

    target = patched_repo / OUR_CHANGE
    data = bytearray(target.read_bytes())
    data[-1] = (data[-1] + 1) % 256  # 只改一个字节
    target.write_bytes(bytes(data))
    assert previs.main(["verify", "--repo", str(patched_repo), "--patch", str(patch_dir)]) == 1

    (patched_repo / OUR_TWEAK).unlink()
    assert previs.main(["verify", "--repo", str(patched_repo), "--patch", str(patch_dir)]) == 1


def test_apply_restores_clean_copy_and_backs_up(previs, patch_dir, clean_repo, capsys):
    """apply 到干净副本能还原我们的文件；被覆盖的原有文件先留 .bak-<时间戳>。"""
    before = (clean_repo / OUR_TWEAK).read_text(encoding="utf-8")
    assert before == UPSTREAM_FILES[OUR_TWEAK]

    assert previs.main(["apply", "--repo", str(clean_repo), "--patch", str(patch_dir)]) == 0
    printed = capsys.readouterr().out
    assert (clean_repo / OUR_CHANGE).read_text(encoding="utf-8") == OUR_PATCHED
    assert (clean_repo / OUR_TWEAK).read_text(encoding="utf-8") == OUR_TWEAKED

    backups = sorted(clean_repo.rglob("*.bak-*"))
    assert [path.name.split(".bak-")[0] for path in backups] == ["config.ts"]
    assert backups[0].read_text(encoding="utf-8") == UPSTREAM_FILES[OUR_TWEAK]
    assert ".bak-" in printed

    # 二次 apply 不该再留一份无意义的备份，也不该报错
    assert previs.main(["apply", "--repo", str(clean_repo), "--patch", str(patch_dir)]) == 0
    assert len(sorted(clean_repo.rglob("*.bak-*"))) == 1


def test_apply_refuses_version_mismatch_without_writing(previs, patch_dir, tmp_path):
    """上游版本不符：拒绝、退出 2、一个文件都不写（包括不建 .bak）。"""
    other = write_repo(tmp_path / "other", files=dict(UPSTREAM_FILES), version="9.9.9")
    before = {path: path.read_bytes() for path in other.rglob("*") if path.is_file()}

    assert previs.main(["apply", "--repo", str(other), "--patch", str(patch_dir)]) == 2

    after = {path: path.read_bytes() for path in other.rglob("*") if path.is_file()}
    assert after == before, "版本不符时不许落任何文件"
    assert not list(other.rglob("*.bak-*"))


def test_apply_rejects_corrupted_patch_before_writing(previs, patch_dir, clean_repo):
    """补丁目录自身损坏（内容与 manifest 的 sha256 对不上）：退出 2 且不写仓库。"""
    payload = patch_dir / "files" / OUR_CHANGE
    payload.write_bytes(b"tampered\n")
    before = {path: path.read_bytes() for path in clean_repo.rglob("*") if path.is_file()}

    assert previs.main(["apply", "--repo", str(clean_repo), "--patch", str(patch_dir)]) == 2
    after = {path: path.read_bytes() for path in clean_repo.rglob("*") if path.is_file()}
    assert after == before


def test_verify_flags_version_drift(previs, patch_dir, tmp_path):
    """仓库换成了新上游：verify 必须把版本不符当漂移（退出 1），而不是只比文件哈希。"""
    newer = write_repo(tmp_path / "newer", files=dict(UPSTREAM_FILES), version="2.0.0")
    assert previs.main(["verify", "--repo", str(newer), "--patch", str(patch_dir)]) == 1


def test_build_dry_run_reports_command_and_dist(previs, tmp_path, capsys):
    """build 走仓库自带的 vite（不依赖 pnpm）；--dry-run 只打印命令与现有 dist 统计。"""
    repo = write_repo(tmp_path / "dist_repo", files={"src/engine.ts": "export {}\n"})
    vite = repo / "node_modules" / "vite" / "bin" / "vite.js"
    vite.parent.mkdir(parents=True)
    vite.write_text("// vite 入口占位\n", encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist" / "index.js").write_bytes(b"a" * 1024)
    (repo / "dist" / "echarts.js").write_bytes(b"b" * 2048)

    node = str(Path(sys.executable))  # dry-run 不执行，随便给个存在的可执行文件
    argv = ["build", "--repo", str(repo), "--node", node, "--dry-run"]
    assert previs.main(argv) == 0
    out = capsys.readouterr().out
    assert "vite" in out and "build" in out
    assert "2 个文件" in out and "3072 字节" in out

    # node_modules 没装（没有 vite）时是环境错误，不是构建失败
    empty = write_repo(tmp_path / "empty_repo", files={"src/engine.ts": "export {}\n"})
    assert previs.main(["build", "--repo", str(empty), "--node", node]) == previs.EXIT_USAGE
