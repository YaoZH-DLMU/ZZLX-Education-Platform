#!/bin/bash
LOG=/tmp/compress_log.txt
echo "=== 压缩开始 $(date) ===" > $LOG
UPLOAD_DIR=/app/app/static/uploads

for f in $(find "$UPLOAD_DIR" -name '*.mp4' -size +20M); do
  size_kb=$(du -k "$f" | cut -f1)
  if [ "$size_kb" -gt 20480 ]; then
    tmp="${f}.compressing.mp4"
    echo "[$(date +%H:%M:%S)] 开始: $f ($(du -sh "$f" | cut -f1))" | tee -a $LOG
    ffmpeg -i "$f" -c:v libx264 -crf 28 -preset medium \
           -c:a aac -b:a 128k -movflags +faststart \
           -threads 2 -y "$tmp" </dev/null 2>> $LOG
    if [ $? -eq 0 ]; then
      mv "$tmp" "$f"
      echo "[$(date +%H:%M:%S)] 完成: $f ($(du -sh "$f" | cut -f1))" | tee -a $LOG
    else
      rm -f "$tmp"
      echo "[$(date +%H:%M:%S)] 失败，保留原文件: $f" | tee -a $LOG
    fi
  fi
done
echo "=== 压缩结束 $(date) ===" | tee -a $LOG
