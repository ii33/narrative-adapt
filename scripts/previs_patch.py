#!/usr/bin/env python3
"""把我们对 vendored DirectorDesk 的改动固化成**可重放**的补丁。

为什么要这个脚本：``.runtime/vendor/director-desk``（不进 git）是原样 vendored 的上游代码，
我们加在上面的预演接口层（v1）与可读性层（v2）只存在于那棵目录树里。上游一升级，这棵树被整棵
覆盖，改动就只有两条路：丢掉，或者凭记忆重做一遍。补丁目录（``patches/director-desk/<version>/``）
把「我们改过/新增的文件」**按上游版本固化完整内容**，于是升级变成一个可核对的动作：
先 ``verify`` 看漂移，确认是新上游、补丁套不上，就手工适配一次，再 ``snapshot`` 固化成新版本。

只有「找文件清单」这一步会用到 git（相对上游 HEAD 的 status）；其余全部是普通文件读写，
所以补丁本身可以离线、无 git 环境重放。

四个子命令::

    snapshot --repo R [--out D] [--files ...] [--force]
        把 R 里被改动/新增的文件导出成补丁目录 D（完整内容 + manifest.json）。
    apply --repo R [--patch D]
        把补丁目录写回 R。先校验上游版本，覆盖前给每个文件写 ``.bak-<时间戳>``。
    verify --repo R [--patch D]
        比较 R 与补丁目录，逐条列出「一致/缺失/被改动」，有漂移就非零退出。
    build --repo R [--node N] [--dry-run]
        用仓库自带的 vite 重建 dist（pnpm 不在 PATH 也没关系），报告产物大小与文件数。

退出码语义（四个子命令统一）::

    0  成功：snapshot 写好了；apply 写完了；verify 全部一致且版本相同；build 产物已生成
    1  漂移或构建失败：verify 发现缺失/被改动/版本不符；build 的 vite 退出码非零
    2  拒绝执行或环境/用法错误：上游版本不符、补丁目录损坏或缺失、参数与路径不合法、
       node/vite 找不到。**apply 返回 2 时保证一个文件都没写。**

全程不联网，也不删用户目录：apply 只覆盖补丁里列出的文件，且先备份；snapshot 只清理
它自己补丁目录下的 ``files/``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = ROOT / ".runtime" / "vendor" / "director-desk"
DEFAULT_PATCH_ROOT = ROOT / "patches" / "director-desk"
MANIFEST_NAME = "manifest.json"
FILES_DIR = "files"
SCHEMA = 1

# 依赖树与构建产物永不打进补丁：体积大、可重建，而且升级后必然整体替换。
SKIP_PARTS = frozenset({".git", "node_modules", "dist", "dist-ssr", "__pycache__", ".vite"})

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_USAGE = 2


class PatchError(Exception):
    """可预期的操作失败：统一转成退出码 2 与一行中文提示，不打栈吓人。"""


# ---------------------------------------------------------------- 基础工具


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve(value: str | Path | None, default: Path | None = None) -> Path:
    """命令行路径按当前工作目录解析；默认值（仓库与补丁根）按工程根解析。"""
    raw = Path(value) if value is not None else default
    if raw is None:
        raise PatchError("缺少路径参数")
    return raw.expanduser().resolve()


def _require_dir(path: Path, what: str) -> Path:
    if not path.is_dir():
        raise PatchError(f"{what}不存在或不是目录：{path}")
    return path


def _display_path(path: Path) -> str:
    """报告里用工程内相对路径，避免把用户名与盘符写进 manifest。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _human_size(size: int) -> str:
    return f"{size / (1024 * 1024):.2f} MB"


def read_upstream_version(repo: Path) -> tuple[str, str]:
    """读 package.json 的 version，返回 (version, shortVersion)。

    版本是这套补丁的唯一身份：补丁里的文件内容是「某个上游版本 + 我们的改动」，套到别的
    版本上会悄悄毁掉升级结果，所以 apply 之前必须核对。shortVersion 只用来拼目录名。
    """
    package = _require_dir(repo, "vendored 仓库") / "package.json"
    if not package.is_file():
        raise PatchError(f"仓库里没有 package.json，无法判断上游版本：{package}")
    try:
        data = json.loads(package.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise PatchError(f"package.json 不是合法 JSON：{exc}") from exc
    version = str(data.get("version") or "").strip()
    if not version:
        raise PatchError(f"package.json 没有 version 字段：{package}")
    short = str(data.get("shortVersion") or "").strip() or version
    return version, short


def patch_dir_name(version: str) -> str:
    """版本号转目录名：'0.5.5+revision.1' 里的 '+' 不适合做目录名。"""
    name = re.sub(r"[^0-9A-Za-z._-]+", "-", version).strip("-")
    return name or "unknown"


def _safe_relative(path: str) -> str:
    """manifest 里的路径最终会写回仓库，绝对路径与 '..' 一律当坏补丁拒绝。"""
    text = str(path).replace("\\", "/").strip()
    parts = text.split("/")
    if not text or text.startswith("/") or ":" in parts[0] or ".." in parts:
        raise PatchError(f"补丁里的路径不合法：{path!r}")
    return text


def _patch_dir_for(args: argparse.Namespace, repo: Path) -> Path:
    """没给 --patch 时，按仓库当前版本推出默认补丁目录（版本不符由调用方报错）。"""
    if args.patch:
        return _require_dir(_resolve(args.patch), "补丁目录")
    _version, short = read_upstream_version(repo)
    candidate = _resolve(args.patch_root, DEFAULT_PATCH_ROOT) / patch_dir_name(short)
    return _require_dir(candidate, f"补丁目录（由仓库版本 {short} 推出）")


# ---------------------------------------------------------------- manifest


def load_manifest(patch_dir: Path) -> dict[str, Any]:
    path = patch_dir / MANIFEST_NAME
    if not path.is_file():
        raise PatchError(f"补丁目录里没有 {MANIFEST_NAME}：{patch_dir}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PatchError(f"{MANIFEST_NAME} 不是合法 JSON：{exc}") from exc
    if data.get("schema") != SCHEMA:
        raise PatchError(f"{MANIFEST_NAME} 的 schema 是 {data.get('schema')!r}，本脚本只认 {SCHEMA}")
    if not str(data.get("upstream_version") or "").strip():
        raise PatchError(f"{MANIFEST_NAME} 里没有 upstream_version")
    entries = data.get("files")
    if not isinstance(entries, list) or not entries:
        raise PatchError(f"{MANIFEST_NAME} 里没有 files 清单")
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("path") or not entry.get("sha256"):
            raise PatchError(f"{MANIFEST_NAME} 的 files 条目缺少 path/sha256：{entry!r}")
        entry["path"] = _safe_relative(entry["path"])
        entry["sha256"] = str(entry["sha256"]).lower()
    return data


def _payload_path(patch_dir: Path, relative: str) -> Path:
    return patch_dir / FILES_DIR / relative


# ---------------------------------------------------------------- snapshot


def detect_changed_files(repo: Path) -> list[str]:
    """用 git status 找出相对上游 HEAD 被改过/新增的文件。

    上游是一次性的 git checkout，所以「我们的改动」就等于「工作区与 HEAD 的差异」。
    这是脚本里唯一依赖 git 的地方，失败时退化成要求 --files 手工列清单。
    """
    git = shutil.which("git")
    if git is None:
        raise PatchError("PATH 里没有 git，无法自动识别改动；请用 --files 显式列出文件")
    completed = subprocess.run(
        [git, "-C", str(repo), "status", "--porcelain", "-z", "--untracked-files=all"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise PatchError(
            "git status 失败，无法自动识别改动；请用 --files 显式列出文件：\n"
            + (completed.stderr or "").strip()
        )
    found: list[str] = []
    records = completed.stdout.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        status, raw = record[:2], record[3:]
        if "R" in status or "C" in status:
            index += 1  # -z 输出里重命名/复制多带一条旧路径记录
        if "D" in status:
            continue  # 只导出「加过/改过」的文件，删除属于上游自己的变化
        relative = raw.replace("\\", "/")
        if any(part in SKIP_PARTS for part in relative.split("/")):
            continue
        found.append(relative)
    return sorted(set(found))


def _tracked_paths(repo: Path) -> set[str] | None:
    """上游 HEAD 里已有的文件集合，用来标 origin=modified / added；拿不到就返回 None。"""
    git = shutil.which("git")
    if git is None:
        return None
    completed = subprocess.run(
        [git, "-C", str(repo), "ls-tree", "-r", "--name-only", "-z", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return None
    return {item.replace("\\", "/") for item in completed.stdout.split("\0") if item}


def _split_files(values: list[str]) -> list[str]:
    """--files 既接受空格分隔，也接受逗号分隔（从 git status 抄出来时更省事）。"""
    collected: list[str] = []
    for value in values:
        for piece in value.replace(",", " ").split():
            collected.append(_safe_relative(piece))
    return sorted(set(collected))


def cmd_snapshot(args: argparse.Namespace) -> int:
    repo = _require_dir(_resolve(args.repo, DEFAULT_REPO), "vendored 仓库")
    version, short = read_upstream_version(repo)
    if args.out:
        out = _resolve(args.out)
    else:
        out = _resolve(args.patch_root, DEFAULT_PATCH_ROOT) / patch_dir_name(short)

    files = _split_files(args.files) if args.files else detect_changed_files(repo)
    if not files:
        raise PatchError("没有找到要导出的文件：仓库看起来和上游一致（或改动都没被 git 看到）")

    tracked = _tracked_paths(repo)
    entries: list[dict[str, Any]] = []
    for relative in files:
        source = repo / relative
        if not source.is_file():
            raise PatchError(f"清单里的文件不存在：{relative}")
        data = source.read_bytes()
        if not data:
            raise PatchError(f"清单里的文件是空的，多半是选错了：{relative}")
        origin = "unknown" if tracked is None else ("modified" if relative in tracked else "added")
        entries.append(
            {
                "path": relative,
                "origin": origin,
                "bytes": len(data),
                "sha256": sha256_bytes(data),
            }
        )

    if out.exists() and any(out.iterdir()) and not args.force:
        raise PatchError(
            f"补丁目录已存在且非空：{out}\n"
            "要覆盖请加 --force（只会清掉该目录自己的 files/ 与 manifest.json，不碰仓库）"
        )

    files_root = out / FILES_DIR
    if files_root.is_dir():
        shutil.rmtree(files_root)
    for entry in entries:
        target = files_root / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / entry["path"], target)

    manifest = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "upstream_version": version,
        "upstream_short_version": short,
        "summary": args.summary
        or (
            "narrative-adapt 预演接口层补丁（v1 宿主接口 + v2 可读性层）。"
            "为什么要改：见 vendored 仓库里的 PATCHES.md 与 docs/PREVIS_HOST_API.md。"
            "本目录由 scripts/previs_patch.py snapshot 生成，不要手改。"
        ),
        "source": {
            "repo": _display_path(repo),
            "detected_by": "--files（手工指定）" if args.files else "git status（相对上游 HEAD）",
        },
        "files": entries,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    total = sum(entry["bytes"] for entry in entries)
    print(f"已固化补丁：{_display_path(out)}（上游 {version}）")
    for entry in entries:
        print(f"  {entry['origin']:<8} {entry['bytes']:>8} 字节  {entry['path']}")
    print(f"\n共 {len(entries)} 个文件，{total} 字节（{_human_size(total)}），清单 {MANIFEST_NAME}")
    return EXIT_OK


# ---------------------------------------------------------------- apply


def cmd_apply(args: argparse.Namespace) -> int:
    repo = _require_dir(_resolve(args.repo, DEFAULT_REPO), "vendored 仓库")
    patch_dir = _patch_dir_for(args, repo)
    manifest = load_manifest(patch_dir)
    patch_version = str(manifest["upstream_version"]).strip()
    version, _short = read_upstream_version(repo)

    if version != patch_version:
        # 拒绝时一个字节都不写：补丁里的文件是「某版上游 + 我们的改动」的完整内容，
        # 套到另一个版本会把上游的新代码整段覆盖掉，比不套更糟。
        print(f"拒绝：这套补丁是针对 {patch_version} 的，当前仓库是 {version}。", file=sys.stderr)
        print(
            "升级上游后请按 docs/PREVIS_PATCH.md 的流程走：先 verify 看漂移 → 手工适配 → "
            "snapshot 固化新版本。本次未写入任何文件。",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # 先整体校验补丁自身（内容与 manifest 的 sha256 对得上），再动仓库：
    # 补丁目录坏了就不该开始写，否则会留下半套文件。
    payloads: dict[str, bytes] = {}
    for entry in manifest["files"]:
        relative = entry["path"]
        source = _payload_path(patch_dir, relative)
        if not source.is_file():
            raise PatchError(f"补丁缺少文件内容：{_display_path(source)}")
        data = source.read_bytes()
        actual = sha256_bytes(data)
        if actual != entry["sha256"]:
            raise PatchError(
                f"补丁内容与 manifest 不符（补丁目录已损坏）：{relative}\n"
                f"  manifest {entry['sha256']}\n  实际     {actual}"
            )
        payloads[relative] = data

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    written: list[str] = []
    backups: list[Path] = []
    for relative, data in payloads.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.read_bytes() == data:
            continue  # 已经一致就不写、也不留无意义的 .bak
        if target.exists():
            backup = target.with_name(f"{target.name}.bak-{stamp}")
            shutil.copy2(target, backup)
            backups.append(backup)
        target.write_bytes(data)
        written.append(relative)

    print(f"已应用补丁：{_display_path(patch_dir)} → {_display_path(repo)}（上游 {version}）")
    if written:
        for relative in written:
            print(f"  写入 {relative}")
    else:
        print("  全部文件已一致，未改动任何内容")
    if backups:
        print(f"覆盖前备份 {len(backups)} 个文件（后缀 .bak-{stamp}）：")
        for backup in backups:
            print(f"  {_display_path(backup)}")
    if not args.no_hint:
        print("\n下一步：python scripts/previs_patch.py build 重建 dist。")
    return EXIT_OK


# ---------------------------------------------------------------- verify


def cmd_verify(args: argparse.Namespace) -> int:
    repo = _require_dir(_resolve(args.repo, DEFAULT_REPO), "vendored 仓库")
    patch_dir = _patch_dir_for(args, repo)
    manifest = load_manifest(patch_dir)
    patch_version = str(manifest["upstream_version"]).strip()
    version, _short = read_upstream_version(repo)

    print(f"仓库：{_display_path(repo)}（{version}）")
    print(f"补丁：{_display_path(patch_dir)}（针对 {patch_version}）")

    version_drift = version != patch_version
    if version_drift:
        print(f"[版本不符] 仓库是 {version}，补丁是针对 {patch_version} 的")

    missing: list[str] = []
    changed: list[str] = []
    same: list[str] = []
    for entry in manifest["files"]:
        relative = entry["path"]
        target = repo / relative
        if not target.is_file():
            missing.append(relative)
            print(f"[缺失]   {relative}")
            continue
        actual = sha256_file(target)
        if actual != entry["sha256"]:
            changed.append(relative)
            print(f"[被改动] {relative}  sha256 {actual[:12]}… ≠ 补丁 {entry['sha256'][:12]}…")
        else:
            same.append(relative)
            print(f"[一致]   {relative}")

    total = len(manifest["files"])
    print(f"\n共 {total} 个文件：一致 {len(same)}，缺失 {len(missing)}，被改动 {len(changed)}")
    if version_drift or missing or changed:
        print("结论：有漂移。要么仓库被改过（先 apply 或重做），要么这是新上游（走升级流程）。")
        return EXIT_DRIFT
    print("结论：仓库与补丁一致。")
    return EXIT_OK


# ---------------------------------------------------------------- build


def _report_dist(dist: Path) -> None:
    if not dist.is_dir():
        print("dist/ 不存在（还没构建过）")
        return
    files = [path for path in dist.rglob("*") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    print(f"dist/：{len(files)} 个文件，{_human_size(total)}（{total} 字节）")
    for path in sorted(files, key=lambda p: p.stat().st_size, reverse=True)[:5]:
        size = path.stat().st_size
        print(f"  {_human_size(size):>10}  {path.relative_to(dist).as_posix()}")


def cmd_build(args: argparse.Namespace) -> int:
    repo = _require_dir(_resolve(args.repo, DEFAULT_REPO), "vendored 仓库")
    vite = repo / "node_modules" / "vite" / "bin" / "vite.js"
    node = args.node or shutil.which("node")
    if not vite.is_file():
        raise PatchError(f"仓库里没有 vite：{_display_path(vite)}（node_modules 没装好？）")
    if not node:
        raise PatchError("找不到 node（PATH 里没有，也没给 --node）；pnpm 不在 PATH 不影响构建")
    command = [str(node), str(vite), "build"]

    if args.dry_run:
        print(f"将执行：{' '.join(command)}（cwd={_display_path(repo)}）")
        _report_dist(repo / "dist")
        return EXIT_OK

    print(f"构建中：{' '.join(command)}（cwd={_display_path(repo)}）")
    completed = subprocess.run(command, cwd=str(repo), check=False)
    if completed.returncode != 0:
        print(f"构建失败：vite build 退出码 {completed.returncode}", file=sys.stderr)
        _report_dist(repo / "dist")
        return EXIT_DRIFT
    _report_dist(repo / "dist")
    return EXIT_OK


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="previs_patch.py",
        description="把 vendored DirectorDesk 的改动做成可重放的补丁（不联网）。",
        epilog="退出码：0 成功/一致；1 有漂移或构建失败；2 拒绝执行或环境/用法错误。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="{snapshot,apply,verify,build}")

    def add_repo(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--repo",
            default=None,
            help=f"vendored 仓库（默认 {_display_path(DEFAULT_REPO)}）",
        )

    def add_patch_root(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--patch-root",
            default=None,
            help=f"补丁根目录（默认 {_display_path(DEFAULT_PATCH_ROOT)}）",
        )

    def add_patch(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--patch",
            default=None,
            help="补丁目录（默认按仓库版本推出 patches/director-desk/<shortVersion>/）",
        )
        add_patch_root(target)

    snapshot = subparsers.add_parser("snapshot", help="把改动导出成补丁目录")
    add_repo(snapshot)
    add_patch_root(snapshot)
    snapshot.add_argument("--out", default=None, help="补丁目录（默认 patches/director-desk/<version>/）")
    snapshot.add_argument(
        "--files",
        nargs="+",
        default=None,
        help="显式列出要导出的文件（相对仓库根，空格或逗号分隔）；省略则用 git status 自动识别",
    )
    snapshot.add_argument("--force", action="store_true", help="补丁目录非空时覆盖")
    snapshot.add_argument("--summary", default=None, help="写进 manifest 的来源说明（一句话）")
    snapshot.set_defaults(handler=cmd_snapshot)

    apply_parser = subparsers.add_parser("apply", help="把补丁目录写回 vendored 仓库")
    add_repo(apply_parser)
    add_patch(apply_parser)
    apply_parser.add_argument("--no-hint", action="store_true", help="不打印下一步提示")
    apply_parser.set_defaults(handler=cmd_apply)

    verify = subparsers.add_parser("verify", help="比较仓库与补丁目录")
    add_repo(verify)
    add_patch(verify)
    verify.set_defaults(handler=cmd_verify)

    build = subparsers.add_parser("build", help="重建 dist 并报告产物")
    add_repo(build)
    build.add_argument("--node", default=None, help="node 可执行文件（默认从 PATH 找）")
    build.add_argument("--dry-run", action="store_true", help="只打印命令与当前 dist 统计，不构建")
    build.set_defaults(handler=cmd_build)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except PatchError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
