import os
import shutil


def clear_pycache(root="."):
    for dirpath, dirnames, filenames in os.walk(root):
        if "__pycache__" in dirnames:
            path = os.path.join(dirpath, "__pycache__")
            try:
                shutil.rmtree(path)
                print("Removed", path)
            except Exception as e:
                print("Skip", path, e)


if __name__ == "__main__":
    clear_pycache(".")
