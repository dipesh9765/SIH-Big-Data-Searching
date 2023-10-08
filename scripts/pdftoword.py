# from PyPDF2 import PdfReader

# reader = PdfReader("84669-pet-vocabulary-list.pdf")
# number_of_pages = len(reader.pages)
# print(number_of_pages)
# for i in range(number_of_pages):
#     page = reader.pages[i]
#     text = page.extract_text()
#     print(text)

from PyPDF2 import PdfReader
from multiprocessing import Process
import sys

def extract(i, end, reader):
    for i in range(end):
        page = reader.pages[i]
        text = page.extract_text()
        text = text.encode("ascii", "ignore")
        text = text.decode()
        print(text)

if __name__ == '__main__':
    reader = PdfReader(sys.argv[1])
    number_of_pages = len(reader.pages)
    processes = []
    pages = number_of_pages/50
    for _ in range(int(pages+1)):
        cp = 0
        if number_of_pages-cp>50:
            process = Process(target=extract, args=(cp,cp+50,reader))
            process.start()
            processes.append()
        else:
            process = Process(target=extract, args=(cp,cp+number_of_pages-cp,reader))
            process.start()
            processes.append(process)
        cp+=50

    for process in processes:
        process.join()


# for i in range(number_of_pages):
#     page = reader.pages[i]
#     text = page.extract_text()
#     text = text.encode("ascii", "ignore")
#     text = text.decode()
#     print(text)