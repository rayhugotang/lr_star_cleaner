

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
    python lr_star_cleaner.py "E:\\Photos\\2025" --apply --trash-dir "E:\\Photos\\.TRASH" --sync-jpg-rating --exiftool "C:\Users\Administrator\Downloads\APP 软件\exiftool-13.36_64\exiftool.exe"
