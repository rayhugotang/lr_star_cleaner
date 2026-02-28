#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lr_star_cleaner.py

Lightroom 星级批量筛选清理脚本（安全版）

规则：
  1★：删除 JPG + RAW
  2★：只保留 JPG，删除 RAW
  3★：只保留 RAW，删除 JPG
  4-5★：同时保留 JPG + RAW
无评级：默认跳过（可用 --include-unrated 将无评级当 1★，危险）

安全策略（默认）：
  - 按“目录 + 文件名(stem)”分组，避免跨目录同名文件混组误删
  - 优先读取 sidecar XMP（组级优先：任意 sidecar 读到星级就不再回退 embedded）
  - 无 sidecar 星级时才回退读取 embedded（分块读取，避免 RAW 全量读入）
  - 检测到星级冲突时，默认整组跳过（除非 --allow-conflicts）
  - 未设置 --trash-dir 时默认禁止永久删除（必须显式 --force-permanent-delete）

用法示例：
  预演：
    python lr_star_cleaner.py "E:\\Photos\\2025" --dry-run --out-dir ".\\out"

  安全执行（移动到回收目录，推荐回收目录在 root 外）：
    python lr_star_cleaner.py "E:\\Photos\\2025" --apply --trash-dir "E:\\_TRASH\\Photos2025" --out-dir ".\\out"

  永久删除（不推荐）：
    python lr_star_cleaner.py "E:\\Photos\\2025" --apply --force-permanent-delete

  同步 JPG 内嵌星级（可选，需要 exiftool）：
    python lr_star_cleaner.py "E:\\Photos\\2025" --dry-run --sync-jpg-rating --exiftool "C:\\Tools\\exiftool\\exiftool.exe"
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Optional, Dict, Any, Set, Tuple, List

# ====== 可按需扩展的后缀集合 ======
RAW_EXTS: Set[str] = {
    ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf",
    ".rw2", ".dng", ".pef", ".srw", ".3fr"
}
JPG_EXTS: Set[str] = {".jpg", ".jpeg"}

# ====== XMP 解析（属性 + 元素 两种写法）======
_RATING_ATTR_RE = re.compile(rb'xmp:Rating\s*=\s*"(-?\d+)"', re.I)
_RATING_ELEM_RE = re.compile(
    rb"<\s*xmp:Rating\s*>\s*(-?\d+)\s*<\s*/\s*xmp:Rating\s*>", re.I
)


def _extract_rating_bytes(xmp_bytes: bytes) -> Optional[int]:
    """
    从 XMP 字节中提取 xmp:Rating。返回 int 或 None。
    """
    m = _RATING_ATTR_RE.search(xmp_bytes)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    m = _RATING_ELEM_RE.search(xmp_bytes)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


def read_xmp_rating_from_sidecar(xmp_path: Path) -> Optional[int]:
    try:
        data = xmp_path.read_bytes()
    except Exception:
        return None
    return _extract_rating_bytes(data)


def read_xmp_rating_from_file_chunked(file_path: Path, max_bytes: int = 8 * 1024 * 1024) -> Optional[int]:
    """
    从 JPG/RAW 文件中尝试提取内嵌 XMP 的星级。
    更安全/更省内存：最多读取 max_bytes（默认 8MB），在读取窗口内寻找 <x:xmpmeta> ... </x:xmpmeta>，
    找不到则在窗口内直接匹配 xmp:Rating。
    """
    try:
        with file_path.open("rb") as f:
            data = f.read(max_bytes)
    except Exception:
        return None

    start = data.find(b"<x:xmpmeta")
    if start != -1:
        end = data.find(b"</x:xmpmeta>", start)
        if end != -1:
            packet = data[start:end + len(b"</x:xmpmeta>")]
            return _extract_rating_bytes(packet)

    return _extract_rating_bytes(data)


def _add_rating_source(entry: Dict[str, Any], source: str, rating: Optional[int]) -> None:
    if rating is None:
        return
    entry["rating_sources"].append(f"{source}={rating}")


def _is_under(child: Path, parent: Path) -> bool:
    """
    child 是否在 parent 之下（包含任意层级）。
    """
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def get_group_key(root: Path, p: Path) -> str:
    """
    安全分组 key：相对目录 + stem（同目录同名视为一组；不同目录不会混组）
    """
    try:
        rel_parent = p.parent.relative_to(root).as_posix().lower()
    except Exception:
        rel_parent = p.parent.as_posix().lower()
    stem = p.stem.lower()
    return f"{rel_parent}/{stem}" if rel_parent not in ("", ".") else stem


def decide_actions(rating: int) -> Optional[str]:
    """
    返回动作类型：
      - "DEL_BOTH": 删除 JPG+RAW
      - "KEEP_JPG": 保留 JPG，删除 RAW
      - "KEEP_RAW": 保留 RAW，删除 JPG
      - "KEEP_BOTH": 保留 JPG+RAW
      - None: 不处理
    """
    if rating == 1:
        return "DEL_BOTH"
    if rating == 2:
        return "KEEP_JPG"
    if rating == 3:
        return "KEEP_RAW"
    if rating in (4, 5):
        return "KEEP_BOTH"
    return None


def find_sidecar_candidates(media_path: Path, for_jpg: bool) -> List[Path]:
    """
    返回可能的 sidecar XMP 文件路径候选（按优先级排序，不保证存在）。

    对 JPG：
      1) foo.jpg.xmp
      2) foo.xmp

    对 RAW：
      1) foo.nef.xmp  (即 原文件名 + ".xmp")
      2) foo.xmp      (同 stem)
    """
    candidates: List[Path] = []
    parent = media_path.parent

    if for_jpg:
        # foo.jpg.xmp
        candidates.append(parent / (media_path.name + ".xmp"))
        # foo.xmp
        candidates.append(parent / (media_path.stem + ".xmp"))
    else:
        # foo.nef.xmp（原名+ .xmp）
        candidates.append(parent / (media_path.name + ".xmp"))
        # foo.xmp（同 stem）
        candidates.append(parent / (media_path.stem + ".xmp"))

    # 去重保持顺序
    seen: Set[str] = set()
    out: List[Path] = []
    for c in candidates:
        k = str(c).lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def _compute_rating(entry: Dict[str, Any], allow_conflicts: bool) -> Tuple[Optional[int], bool]:
    """
    返回 (rating, has_conflict)

    组级策略：
      1) 只要任意 sidecar 存在并读到 rating，则忽略全部 embedded 回退
      2) 只有完全没有 sidecar rating 时，才读取 embedded（JPG 优先，其次 RAW）
    冲突默认更安全：has_conflict=True 时，除非 allow_conflicts，否则返回 rating=None。
    """
    # 1) sidecar rating（组级优先）
    sidecar_vals: List[int] = []
    for s in entry["rating_sources"]:
        if s.startswith("JPG-XMP:") or s.startswith("RAW-XMP:"):
            try:
                v = int(s.split("=", 1)[1])
                if 0 <= v <= 5:
                    sidecar_vals.append(v)
            except Exception:
                pass

    if sidecar_vals:
        uniq = sorted(set(sidecar_vals))
        if len(uniq) == 1:
            return uniq[0], False
        entry["rating_sources"].append("CONFLICT:SIDECAR_MULTI_VALUES=" + ",".join(map(str, uniq)))
        return (uniq[0] if allow_conflicts else None), True

    # 2) embedded fallback（无 sidecar rating 才回退）
    embedded_sources: List[Tuple[str, Optional[int]]] = []
    if entry["jpgs"]:
        jpg = entry["jpgs"][0]
        embedded_sources.append((f"JPG-EMBED:{jpg.name}", read_xmp_rating_from_file_chunked(jpg)))
    if entry["raws"]:
        raw = entry["raws"][0]
        embedded_sources.append((f"RAW-EMBED:{raw.name}", read_xmp_rating_from_file_chunked(raw)))

    for src, val in embedded_sources:
        _add_rating_source(entry, src, val)

    vals = [v for (_s, v) in embedded_sources if v is not None and 0 <= v <= 5]
    if not vals:
        return None, False

    uniq = sorted(set(vals))
    if len(uniq) == 1:
        return uniq[0], False

    entry["rating_sources"].append("CONFLICT:EMBED_MULTI_VALUES=" + ",".join(map(str, uniq)))
    return (uniq[0] if allow_conflicts else None), True


def run_exiftool_copy_rating(exiftool_cmd: str, xmp_path: Path, jpg_path: Path) -> bool:
    """
    使用 exiftool 将 xmp_path 中的 XMP:Rating 写入 jpg_path（覆盖原文件）。
    """
    cmd = [
        exiftool_cmd,
        "-overwrite_original",
        f"-tagsFromFile={str(xmp_path)}",
        "-XMP:Rating",
        str(jpg_path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.returncode == 0
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lightroom 星级批量筛选清理（安全版：目录分组、冲突跳过、默认禁永久删）"
    )
    parser.add_argument("root", help="照片根目录（递归处理）")

    parser.add_argument("--apply", action="store_true", help="实际执行删除/移动（默认不执行）")
    parser.add_argument("--dry-run", action="store_true", help="只预演（默认即预演）")

    parser.add_argument("--trash-dir", default="",
                        help="删除改为移动到此目录（推荐），例如 E:\\_TRASH\\Photos2025")
    parser.add_argument("--force-permanent-delete", action="store_true",
                        help="允许在未设置 --trash-dir 时永久删除（强烈不建议）。默认不允许。")

    parser.add_argument("--include-unrated", action="store_true",
                        help="将无评级视为 1★（危险，不建议）")

    parser.add_argument("--out-dir", default=".",
                        help="输出目录（CSV 等），默认当前目录。建议不要放在 root 内部。")
    parser.add_argument("--csv", default="delete_plan.csv",
                        help="导出计划 CSV（默认 delete_plan.csv）")
    parser.add_argument("--conflicts-csv", default="rating_conflicts.csv",
                        help="导出星级冲突明细 CSV（默认 rating_conflicts.csv）")

    parser.add_argument("--allow-conflicts", action="store_true",
                        help="允许冲突组继续执行（不安全）。默认：有冲突整组跳过。")

    parser.add_argument("--exclude-dirs", action="append", default=[],
                        help="排除目录（可重复）。示例：--exclude-dirs .TRASH --exclude-dirs output")
    parser.add_argument("--max-files", type=int, default=0,
                        help="最多扫描文件数（0=不限制），防止误扫整个硬盘。")
    parser.add_argument("--max-groups", type=int, default=0,
                        help="最多处理分组数（0=不限制），防止误扫整个硬盘。")

    parser.add_argument("--sync-jpg-rating", action="store_true",
                        help="当保留 JPG 且 JPG 内嵌 rating 为空/0，且 sidecar 有 1-5 星时写入内嵌（需 exiftool）")
    parser.add_argument("--exiftool", default="exiftool",
                        help="exiftool 可执行程序路径（默认从 PATH 查找）")

    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"路径不存在：{root}")
        sys.exit(1)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    trash_dir_resolved = Path(args.trash_dir).resolve() if args.trash_dir else None

    # 排除目录集合（resolve 后比对）
    exclude_abs: Set[Path] = set()
    for d in args.exclude_dirs:
        p = Path(d)
        if not p.is_absolute():
            p = (root / p)
        try:
            exclude_abs.add(p.resolve())
        except Exception:
            exclude_abs.add(p)

    # 如果 trash-dir 在 root 内，必须排除它避免二次扫描
    if trash_dir_resolved and _is_under(trash_dir_resolved, root):
        exclude_abs.add(trash_dir_resolved)

    index: Dict[str, Dict[str, Any]] = {}  # key -> {"jpgs":[],"raws":[],"rating_sources":[]}

    print("扫描文件中……")
    scanned_files = 0

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        ext = path.suffix.lower()

        # 不直接遍历 .xmp，本工具通过 JPG/RAW 去找边车
        if ext == ".xmp":
            continue

        # 排除目录（命中任意祖先即跳过）
        try:
            rp = path.resolve()
        except Exception:
            rp = path

        skip = False
        for ex in exclude_abs:
            if _is_under(rp, ex):
                skip = True
                break
        if skip:
            continue

        if ext not in JPG_EXTS and ext not in RAW_EXTS:
            continue

        key = get_group_key(root, path)
        entry = index.setdefault(key, {"jpgs": [], "raws": [], "rating_sources": []})

        if ext in JPG_EXTS:
            entry["jpgs"].append(path)
            # 收集 sidecar rating（组级优先；embedded 回退到决策阶段统一做）
            for sc in find_sidecar_candidates(path, for_jpg=True):
                if sc.exists():
                    r2 = read_xmp_rating_from_sidecar(sc)
                    _add_rating_source(entry, f"JPG-XMP:{sc.name}", r2)

        elif ext in RAW_EXTS:
            entry["raws"].append(path)
            for sc in find_sidecar_candidates(path, for_jpg=False):
                if sc.exists():
                    r = read_xmp_rating_from_sidecar(sc)
                    _add_rating_source(entry, f"RAW-XMP:{sc.name}", r)

        scanned_files += 1
        if args.max_files and scanned_files >= args.max_files:
            print(f"[保护触发] 已达到 --max-files={args.max_files}，停止扫描。")
            break

    if args.max_groups and len(index) > args.max_groups:
        keys = sorted(index.keys())[:args.max_groups]
        index = {k: index[k] for k in keys}
        print(f"[保护触发] 已达到 --max-groups={args.max_groups}，仅处理前 {args.max_groups} 组。")

    print("制定删除/保留计划……")
    to_delete: List[Tuple[Path, int]] = []
    to_keep: List[Tuple[Path, int]] = []
    skipped: List[Tuple[Path, str]] = []
    conflicts: List[Tuple[str, str]] = []

    # 逐组决策
    for key, entry in index.items():
        rating, has_conflict = _compute_rating(entry, allow_conflicts=args.allow_conflicts)

        # 无评级且用户要求当作 1★
        if (rating is None or rating == 0) and args.include_unrated:
            rating = 1
            entry["rating_sources"].append("FORCE_UNRATED_AS_1STAR")

        # 冲突处理
        if has_conflict or any(s.startswith("CONFLICT:") for s in entry["rating_sources"]):
            conflicts.append((key, "; ".join(entry["rating_sources"])))
            if not args.allow_conflicts:
                for p in entry["jpgs"] + entry["raws"]:
                    skipped.append((p, "RATING_CONFLICT_SKIP_GROUP"))
                continue

        # 无评级 -> 跳过
        if rating is None or rating == 0:
            for p in entry["jpgs"] + entry["raws"]:
                skipped.append((p, "UNRATED"))
            continue

        action = decide_actions(rating)
        if action is None:
            for p in entry["jpgs"] + entry["raws"]:
                skipped.append((p, f"UNHANDLED_RATING:{rating}"))
            continue

        if action == "DEL_BOTH":
            for p in entry["jpgs"] + entry["raws"]:
                to_delete.append((p, rating))

        elif action == "KEEP_JPG":
            for p in entry["jpgs"]:
                to_keep.append((p, rating))
            for p in entry["raws"]:
                to_delete.append((p, rating))

        elif action == "KEEP_RAW":
            for p in entry["raws"]:
                to_keep.append((p, rating))
            for p in entry["jpgs"]:
                to_delete.append((p, rating))

        elif action == "KEEP_BOTH":
            for p in entry["jpgs"] + entry["raws"]:
                to_keep.append((p, rating))

    # 导出计划 CSV
    csv_path = out_dir / args.csv
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Action", "Rating", "Path"])
        for p, r in to_delete:
            writer.writerow(["DELETE", r, str(p)])
        for p, r in to_keep:
            writer.writerow(["KEEP", r, str(p)])
        for p, reason in skipped:
            writer.writerow(["SKIP", reason, str(p)])
    print(f"计划已导出：{csv_path.resolve()}")

    # 导出冲突明细（若有）
    if conflicts:
        conf_csv = out_dir / args.conflicts_csv
        with conf_csv.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["GroupKey", "Sources"])
            for k, s in conflicts:
                writer.writerow([k, s])
        print(f"检测到 {len(conflicts)} 处星级冲突，详见：{conf_csv.resolve()}")

    print(f"统计：DELETE={len(to_delete)}  KEEP={len(to_keep)}  SKIP={len(skipped)}")

    # ========== 可选：同步 JPG 的内嵌 rating ==========
    synced_count = 0
    if args.sync_jpg_rating:
        print("开始同步 JPG 内嵌 rating（仅针对“计划保留”的 JPG）……")
        for jpg, _r in to_keep:
            if jpg.suffix.lower() not in JPG_EXTS:
                continue

            # 已有内嵌星级就跳过
            jpg_embed_rating = read_xmp_rating_from_file_chunked(jpg)
            if jpg_embed_rating is not None and jpg_embed_rating > 0:
                continue

            # 找到 sidecar（JPG 优先：foo.jpg.xmp -> foo.xmp）
            sidecar_path: Optional[Path] = None
            sidecar_rating: Optional[int] = None
            for sc in find_sidecar_candidates(jpg, for_jpg=True):
                if sc.exists():
                    r2 = read_xmp_rating_from_sidecar(sc)
                    if r2 is not None and 1 <= r2 <= 5:
                        sidecar_path = sc
                        sidecar_rating = r2
                        break

            if sidecar_path is None or sidecar_rating is None:
                continue

            ok = run_exiftool_copy_rating(args.exiftool, sidecar_path, jpg)
            if ok:
                synced_count += 1
            else:
                print(f"[同步失败] {jpg}  <-  {sidecar_path}")

        print(f"JPG rating 同步完成：{synced_count} 个文件写入成功。")

    # 预演：不执行删除/移动
    if not args.apply or args.dry_run:
        print("Dry-run：未实际删除任何文件。确认 CSV 后再加 --apply 执行。")
        return

    # 真正执行删除 / 移动到回收目录
    if args.trash_dir:
        trash_dir = Path(args.trash_dir)
        trash_dir.mkdir(parents=True, exist_ok=True)
        print(f"删除操作改为移动到回收目录：{trash_dir}")

        moved = 0
        for p, _r in to_delete:
            try:
                # 尽量保留原相对路径层级
                try:
                    rel = p.relative_to(root)
                except Exception:
                    rel = Path(p.name)
                target = trash_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(target))
                moved += 1
            except Exception as e:
                print(f"[移动失败] {p} -> {target} : {e}")

        print(f"已移动到回收目录：{moved}/{len(to_delete)}")
        return

    # 永久删除（默认禁止）
    if not args.force_permanent_delete:
        print("安全保护：未设置 --trash-dir，默认不允许永久删除。")
        print("如确需永久删除，请在确认 CSV 无误后加：--force-permanent-delete")
        return

    deleted = 0
    for p, _r in to_delete:
        try:
            os.remove(p)
            deleted += 1
        except Exception as e:
            print(f"[删除失败] {p} : {e}")

    print(f"已永久删除：{deleted}/{len(to_delete)}")


if __name__ == "__main__":
    main()