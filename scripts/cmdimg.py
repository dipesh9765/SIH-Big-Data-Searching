import os
import sys

os.system('tesseract.exe "'+sys.argv[1]+'" ./scripts/output -l eng')

f = open("output.txt", "r")
print(f.read())