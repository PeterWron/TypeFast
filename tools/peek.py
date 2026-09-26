"""打印指定行区间的原始内容（带行号与 repr），排查文档损坏。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = os.path.join(ROOT, sys.argv[1])
start = int(sys.argv[2])
end = int(sys.argv[3])
lines = open(path, encoding="utf-8").read().split(chr(10))
for i in range(start - 1, min(end, len(lines))):
    print("%4d %s" % (i + 1, repr(lines[i])))
