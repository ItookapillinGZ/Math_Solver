import os

def list_tree(path, depth=0):
    try:
        entries = os.listdir(path)
        for e in sorted(entries):
            full = os.path.join(path, e)
            print("  " * depth + e)
            if os.path.isdir(full) and depth < 6:
                list_tree(full, depth + 1)
    except Exception as ex:
        print("  " * depth + f"[ERROR: {ex}]")

list_tree(".")
