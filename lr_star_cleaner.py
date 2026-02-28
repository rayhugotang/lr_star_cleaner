# -*- coding: utf-8 -*-
"""
Lightroom 星级批量筛选删除脚本（优先边车 XMP + 可选 JPG 同步）
规则：
  1★：删除 JPG + RAW
  2★：只保留 JPG，删除 RAW
  3★：只保留 RAW，删除 JPG
  4-5★：同时保留 JPG + RAW
无评级：默认跳过（可用 --include-unrated 把无评级当 1★ 处理，危险）

关键策略：
- 只要存在边车 .xmp（JPG 或 RAW 的），就只取边车里的 xmp:Rating，完全忽略文件内嵌 rating（哪怕是 0）。
- 仅当不存在任意边车时，才回退去读文件内嵌 XMP。
- 【新增】--sync-jpg-rating：当决定“保留 JPG”时，如果 JPG 内嵌 rating 为空/0 且存在同名 XMP 中有 1–5 星，
  自动用 exiftool 把 XMP 的 rating（和 label）写入 JPG 内部。

用法示例：
  预演：
    python lr_star_cleaner.py "E:\\Photos\\2025" --dry-run
  真删（建议移动到回收目录）+ 同步 JPG 星级：
    python lr_star_cleaner.py "E:\\Photos\\2025" --apply --trash-dir "E:\\Photos\\.TRASH" --sync-jpg-rating --exiftool "C:\\Tools\\exiftool\\exiftool.exe"
"""

import argparse
import csv
import os
from pathlib import Path
import re
import shutil
import sys
import subprocess

# ====== 可按需扩展的后缀集合 ======
RAW_EXTS = {
    '.cr2', '.cr3', '.nef', '.arw', '.raf', '.orf',
    '.rw2', '.dng', '.pef', '.srw', '.3fr'
}
JPG_EXTS = {'.jpg', '.jpeg'}

# ====== XMP 解析（属性 + 元素 两种写法）======
_RATING_ATTR_RE = re.compile(rb'xmp:Rating\s*=\s*"(-?\d+)"', re.I)
_RATING_ELEM_RE = re.compile(
    rb'<\s*xmp:Rating\s*>\s*(-?\d+)\s*<\s*/\s*xmp:Rating\s*>', re.I
)

def _extract_rating_bytes(xmp_bytes: bytes):
    """
    从一段 XMP 字节中提取星级：
      1) 属性: xmp:Rating="2"
      2) 元素: <xmp:Rating>2</xmp:Rating>
    返回 int (0..5) 或 None
    """
    m = _RATING_ATTR_RE.search(xmp_bytes) or _RATING_ELEM_RE.search(xmp_bytes)
    if not m:
        return None
    try:
        val = int(m.group(1))
        return val if 0 <= val <= 5 else None
    except Exception:
        return None

def read_xmp_rating_from_sidecar(xmp_path: Path):
    try:
        data = xmp_path.read_bytes()
    except Exception:
        return None
    return _extract_rating_bytes(data)

def read_xmp_rating_from_file(file_path: Path):
    """
    从 JPG/RAW 文件中尝试提取内嵌 XMP 的星级。
    简化实现：直接在文件字节里搜 <x:xmpmeta> ... </x:xmpmeta> 区段；找不到则全文件搜。
    """
    try:
        data = file_path.read_bytes()
    except Exception:
        return None
    start = data.find(b"<x:xmpmeta")
    end = data.find(b"</x:xmpmeta>", start) if start != -1 else -1
    packet = data[start:end+len(b"</x:xmpmeta>")] if start != -1 and end != -1 else data
    return _extract_rating_bytes(packet)

def _assign_rating(entry, source: str, rating):
    """
    entry: {"jpgs": [], "raws": [], "rating": int|None, "rating_sources": [str]}
    """
    if rating is None:
        return
    if entry["rating"] is not None and entry["rating"] != rating:
        entry["rating_sources"].append(f"CONFLICT:{source}={rating}")
    else:
        entry["rating"] = rating
        entry["rating_sources"].append(f"{source}={rating}")

def get_basename_key(p: Path):
    # 以文件名（不含扩展名）的小写作为 key（DSC_0001.* 视为同一组）
    return p.stem.lower()

def decide_actions(rating: int):
    """
    返回 (keep_jpg, keep_raw)
    """
    if rating == 1:
        return (False, False)  # 删除全部
    if rating == 2:
        return (True, False)   # 仅留 JPG
    if rating == 3:
        return (False, True)   # 仅留 RAW
    if rating in (4, 5):
        return (True, True)    # 都留
    return None  # 其它值不处理

def find_sidecar_candidates(basename: Path, for_jpg: bool):
    """
    根据基名生成可能的边车路径列表（按优先级顺序）。
    for_jpg=True  -> 针对 JPG 的 sidecar 搜索顺序
    for_jpg=False -> 针对 RAW 的 sidecar 搜索顺序
    """
    parent = basename.parent
    stem = basename.stem  # 不含扩展名
    suffix = basename.suffix  # 含点

    candidates = []
    if for_jpg:
        # JPG 优先：同名.jpg.xmp -> 同名.xmp -> 同名.<raw>.xmp（兼容你的历史写法）
        candidates.append(parent / f"{stem}{suffix}.xmp")   # DSC_0001.JPG.xmp
        candidates.append(parent / f"{stem}.xmp")           # DSC_0001.xmp
        for rx in RAW_EXTS:
            candidates.append(parent / f"{stem}{rx}.xmp")   # DSC_0001.NEF.xmp / .CR3.xmp ...
    else:
        # RAW 优先：同名.rawExt.xmp -> 同名.xmp
        candidates.append(parent / f"{stem}{suffix}.xmp")   # DSC_0001.NEF.xmp
        candidates.append(parent / f"{stem}.xmp")           # DSC_0001.xmp
    return candidates

def run_exiftool_copy_rating(exiftool_cmd: str, xmp_path: Path, jpg_path: Path, copy_label: bool = True):
    """
    调用 exiftool 把 xmp:rating（和 label）写入 JPG。
    返回 True/False 表示是否成功。
    """
    if not xmp_path.exists() or not jpg_path.exists():
        return False
    args = [exiftool_cmd, "-overwrite_original", "-tagsfromfile", str(xmp_path), "-xmp:rating"]
    if copy_label:
        args.append("-xmp:label")
    args.append(str(jpg_path))

    try:
        res = subprocess.run(args, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"[exiftool失败] {jpg_path.name} <- {xmp_path.name} : {res.stderr.strip() or res.stdout.strip()}")
            return False
        return True
    except FileNotFoundError:
        print("[exiftool未找到] 请通过 --exiftool 指定路径，或把 exiftool 加到 PATH。")
        return False
    except Exception as e:
        print(f"[exiftool异常] {jpg_path.name} <- {xmp_path.name} : {e}")
        return False

def main():
    parser = argparse.ArgumentParser(
        description="Lightroom 星级批量筛选删除（1删全，2留JPG，3留RAW，4-5都留；优先边车 XMP；可选同步 JPG 内嵌rating）"
    )
    parser.add_argument("root", help="照片根目录（递归处理）")
    parser.add_argument("--apply", action="store_true", help="实际删除（默认不删除）")
    parser.add_argument("--dry-run", action="store_true", help="只预演（默认即预演）")
    parser.add_argument("--trash-dir", default="",
                        help="删除改为移动到此目录，建议设置，例如 E:\\Photos\\.TRASH")
    parser.add_argument("--include-unrated", action="store_true",
                        help="将无评级视为 1★（危险，不建议）")
    parser.add_argument("--csv", default="delete_plan.csv",
                        help="导出计划 CSV（默认 delete_plan.csv）")
    parser.add_argument("--conflicts-csv", default="rating_conflicts.csv",
                        help="导出星级冲突明细 CSV（默认 rating_conflicts.csv）")
    # 新增：同步 JPG rating
    parser.add_argument("--sync-jpg-rating", action="store_true",
                        help="当保留JPG且JPG内嵌rating为空/0，且同名XMP存在1-5星时，把XMP的rating写入JPG内嵌XMP")
    parser.add_argument("--exiftool", default="exiftool",
                        help="exiftool 可执行程序路径（默认从 PATH 查找）")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"路径不存在：{root}")
        sys.exit(1)

    index = {}  # key -> {"jpgs": [], "raws": [], "rating": int|None, "rating_sources": []}

    print("扫描文件中……")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        ext = path.suffix.lower()

        # 不单独遍历孤立 .xmp；在 JPG/RAW 分支里按规则寻找各自边车
        if ext == ".xmp":
            continue

        key = get_basename_key(path)
        entry = index.setdefault(key, {"jpgs": [], "raws": [], "rating": None, "rating_sources": []})

        if ext in JPG_EXTS:
            entry["jpgs"].append(path)

            # 先查 JPG 边车：优先 xxx.JPG.xmp，其次 xxx.xmp，再兼容 xxx.<raw>.xmp
            sc_candidates = find_sidecar_candidates(path, for_jpg=True)
            sidecar_found = False
            for sc in sc_candidates:
                if sc.exists():
                    r2 = read_xmp_rating_from_sidecar(sc)
                    _assign_rating(entry, f"JPG-XMP:{sc.name}", r2)
                    sidecar_found = True
                    # 不break，保留冲突信息（若多个边车存在且不一致）

            # 只有在 **没有任意 JPG 边车** 的情况下，才回退读内嵌
            if not sidecar_found:
                r = read_xmp_rating_from_file(path)
                _assign_rating(entry, f"JPG-EMBED:{path.name}", r)

        elif ext in RAW_EXTS:
            entry["raws"].append(path)

            # RAW 的边车：优先 .rawExt.xmp（如 .NEF.xmp），其次同名 .xmp
            sc_candidates = find_sidecar_candidates(path, for_jpg=False)
            sidecar_found = False
            for sc in sc_candidates:
                if sc.exists():
                    r = read_xmp_rating_from_sidecar(sc)
                    _assign_rating(entry, f"RAW-XMP:{sc.name}", r)
                    sidecar_found = True
                    # 不break，保留冲突信息

            # 只有在 **没有任意 RAW 边车** 的情况下，才回退读内嵌（DNG 等少数情况）
            if not sidecar_found:
                r_embed = read_xmp_rating_from_file(path)
                _assign_rating(entry, f"RAW-EMBED:{path.name}", r_embed)

        else:
            continue

    print("制定删除/保留计划……")
    to_delete = []
    to_keep = []
    skipped = []
    conflicts = []

    # 逐组决策
    for key, entry in index.items():
        rating = entry["rating"]

        # 无评级且用户要求当作 1★
        if (rating is None or rating == 0) and args.include_unrated:
            rating = 1
            entry["rating_sources"].append("FORCE_UNRATED_AS_1STAR")

        # 记录冲突
        if any(s.startswith("CONFLICT:") for s in entry["rating_sources"]):
            conflicts.append((key, "; ".join(entry["rating_sources"])))

        # 无评级 -> 跳过
        if rating is None or rating == 0:
            for p in entry["jpgs"] + entry["raws"]:
                skipped.append((p, "UNRATED"))
            continue

        decision = decide_actions(rating)
        if decision is None:
            for p in entry["jpgs"] + entry["raws"]:
                skipped.append((p, f"UNKNOWN_RATING_{rating}"))
            continue

        keep_jpg, keep_raw = decision

        # 计划 JPG
        for jpg in entry["jpgs"]:
            if keep_jpg:
                to_keep.append((jpg, rating))
            else:
                to_delete.append((jpg, rating))
        # 计划 RAW
        for raw in entry["raws"]:
            if keep_raw:
                to_keep.append((raw, rating))
            else:
                to_delete.append((raw, rating))

    # 导出计划 CSV
    csv_path = Path(args.csv)
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
        conf_csv = Path(args.conflicts_csv)
        with conf_csv.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["BasenameKey", "Sources"])
            for k, s in conflicts:
                writer.writerow([k, s])
        print(f"检测到 {len(conflicts)} 处星级冲突，详见：{conf_csv.resolve()}")

    print(f"将删除 {len(to_delete)} 个文件，保留 {len(to_keep)} 个文件，跳过 {len(skipped)} 个文件。")

    # ========== 【新增】在真正删除前，同步 JPG 的内嵌 rating ==========
    synced_count = 0
    if args.sync_jpg_rating:
        print("开始同步 JPG 内嵌 rating（仅针对“计划保留”的 JPG）……")
        # 为了避免无谓写入，仅在“计划保留 JPG”的条目上执行
        for jpg, _r in to_keep:
            if jpg.suffix.lower() not in JPG_EXTS:
                continue

            # 读取 JPG 内嵌 rating
            jpg_embed_rating = read_xmp_rating_from_file(jpg)
            if jpg_embed_rating is not None and jpg_embed_rating > 0:
                continue  # 已有 1-5 星，无需同步

            # 查找可用的 sidecar，先 JPG 自己的，再 RAW 写法
            sc_candidates = find_sidecar_candidates(jpg, for_jpg=True)
            xmp_rating = None
            chosen_sc = None
            for sc in sc_candidates:
                if sc.exists():
                    r2 = read_xmp_rating_from_sidecar(sc)
                    if r2 is not None and 1 <= r2 <= 5:
                        xmp_rating = r2
                        chosen_sc = sc
                        break

            if xmp_rating is None or chosen_sc is None:
                continue  # 没有有效 sidecar 星级

            # 调用 exiftool 写入 JPG
            ok = run_exiftool_copy_rating(args.exiftool, chosen_sc, jpg, copy_label=True)
            if ok:
                synced_count += 1

        print(f"JPG rating 同步完成：{synced_count} 个文件写入成功。")

    # 预演：不删除
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
    else:
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
