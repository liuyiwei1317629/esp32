#!/bin/bash
# 构建期转码：webm(VP9+alpha) -> 320x180 JPEG 帧序列（黑底）。
# 在 WSL 里跑（Windows 上没有 ffmpeg）；由 bridge/tools/build_screen_frames.py 调用。
set -euo pipefail

SRC="/mnt/c/Users/DELL/Desktop/dsh项目记忆后端+pet/cute-fat-fish-pet-1.2.3-win-x64/app/assets/webm"
OUT="/mnt/c/Users/DELL/voice-ai/bridge/screen_frames"
LIST="$(dirname "$0")/frame_names.txt"
LOG="$OUT/build.log"

mkdir -p "$OUT"
: > "$LOG"

while IFS= read -r name; do
  if [ -z "$name" ]; then
    continue
  fi
  tmp="$OUT/.tmp_$name"
  rm -rf "$tmp"
  mkdir -p "$tmp"
  # 源是 tv 范围（黑 = Y16）且没有 range 元数据，ffmpeg 默认按 pc 解释，
  # 黑底会变成 #101010。必须显式 in_range=tv:out_range=pc。
  ffmpeg -nostdin -loglevel error -y -i "$SRC/$name.webm" -an \
    -vf "fps=10,scale=320:180:flags=lanczos:in_range=tv:out_range=pc,format=yuvj420p" \
    -q:v 3 -start_number 1 "$tmp/%04d.jpg"
  rm -rf "$OUT/$name"
  mv "$tmp" "$OUT/$name"
  echo "$name $(ls "$OUT/$name" | wc -l)" >> "$LOG"
done < "$LIST"

echo "ALL_DONE" >> "$LOG"
