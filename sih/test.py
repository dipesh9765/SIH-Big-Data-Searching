import mmap

with open(r'C:\Users\HP\OneDrive\Desktop\sihreact\test.txt', 'rb', 0) as file:
    s = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
    if s.find(b'') != -1:
        print(1)
    else:
        print(0)