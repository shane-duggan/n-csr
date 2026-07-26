import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
for path in (os.path.join(ROOT, "src"), os.path.join(ROOT, "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)
