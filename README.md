# Lightroom 星级批量清理工具（lr_star_cleaner）

根据 Lightroom 星级自动清理 JPG / RAW 文件。

支持：

-   优先读取 sidecar XMP
-   无 sidecar 时读取内嵌 XMP（安全分块读取）
-   自动按目录分组（避免跨目录误删）
-   冲突默认跳过（更安全）
-   默认不允许永久删除
-   可移动到回收目录
-   可选同步 JPG 内嵌星级（需 exiftool）

------------------------------------------------------------------------

## 清理规则

| 星级 | JPG | RAW |
|------|-----|-----|
| 1★   | 删除 | 删除 |
| 2★   | 保留 | 删除 |
| 3★   | 删除 | 保留 |
| 4★   | 保留 | 保留 |
| 5★   | 保留 | 保留 |
| 无评级 | 默认跳过 | - |

------------------------------------------------------------------------

## ⚠️ 安全说明

-   默认仅 dry-run，不会真正删除
-   默认不允许永久删除
-   建议先完整备份
-   冲突分组默认跳过

------------------------------------------------------------------------

## 基本用法

### 预演

python lr_star_cleaner.py "E:`\Photos`{=tex}\\2025" --dry-run --out-dir
".`\out`{=tex}"

### 安全执行（移动到回收目录）

python lr_star_cleaner.py "E:`\Photos`{=tex}\\2025" --apply --trash-dir
"E:\_TRASH`\Photos2025`{=tex}" --out-dir ".`\out`{=tex}"

### 永久删除（不推荐）

python lr_star_cleaner.py "E:`\Photos`{=tex}\\2025" --apply
--force-permanent-delete

------------------------------------------------------------------------

# English Version

# Lightroom Rating Cleaner (lr_star_cleaner)

Automatically clean JPG / RAW files based on Lightroom star ratings.

Safety-first design.

------------------------------------------------------------------------

## Features

-   Prioritizes sidecar XMP files
-   Falls back to embedded XMP (chunk-safe reading)
-   Groups by directory + filename
-   Conflict groups skipped by default
-   Permanent delete disabled by default
-   Optional move-to-trash workflow
-   Optional JPG rating sync (via ExifTool)

------------------------------------------------------------------------

## Rating Rules

| Rating | JPG | RAW |
|------|-----|-----|
| 1★   | DELETE | DELETE |
| 2★   | KEEP | DELETE |
| 3★   | DELETE | KEEP |
| 4★   | KEEP | KEEP |
| 5★   | KEEP | KEEP |
| Unrated | Skip | - |

------------------------------------------------------------------------

## Basic Usage

### Dry Run

python lr_star_cleaner.py "E:`\Photos`{=tex}\\2025" --dry-run --out-dir
".`\out`{=tex}"

### Safe Execution

python lr_star_cleaner.py "E:`\Photos`{=tex}\\2025" --apply --trash-dir
"E:\_TRASH`\Photos2025`{=tex}"

### Permanent Delete (Not Recommended)

python lr_star_cleaner.py "E:`\Photos`{=tex}\\2025" --apply
--force-permanent-delete

------------------------------------------------------------------------

## Disclaimer

This tool may cause data loss.\
Use at your own risk.
