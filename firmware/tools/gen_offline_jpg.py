# -*- coding: utf-8 -*-
"""把离线图 JPEG 转成 C 数组。用法：python gen_offline_jpg.py <jpg> <out.h>"""
import sys


def main(src, dst):
    with open(src, "rb") as fh:
        blob = fh.read()
    if len(blob) > 40000:
        raise SystemExit("too big: %d B" % len(blob))
    lines = ["// 自动生成，别手改：firmware/tools/gen_offline_jpg.py",
             "#pragma once",
             "#include <stdint.h>",
             "#include <stddef.h>",
             "",
             "const uint8_t kOfflineJpg[] = {"]
    for i in range(0, len(blob), 16):
        lines.append("  " + ",".join("0x%02X" % b for b in blob[i:i + 16]) + ",")
    lines += ["};", "const size_t kOfflineJpgLen = %d;" % len(blob), ""]
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("wrote %s (%d B)" % (dst, len(blob)))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
