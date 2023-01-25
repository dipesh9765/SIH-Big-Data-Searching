import PyPDF2
import time;
st = time.time()
pdfFileObj = open(r'//FILE PATH', 'rb') 
pdfReader = PyPDF2.PdfFileReader(pdfFileObj)
numOfPages = pdfReader.numPages
print(numOfPages)
for x in range(numOfPages):
    pageObj = pdfReader.getPage(x)
    pageObj.extractText() 
pdfFileObj.close() 
end = time.time()
print(end-st)