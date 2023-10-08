from pdf2image import convert_from_path
import numpy as np
from pdf2image.exceptions import (
    PDFInfoNotInstalledError,
    PDFPageCountError,
    PDFSyntaxError
)
import sys
import easyocr

images = convert_from_path(sys.argv[1],poppler_path="C:/Program Files/poppler-21.11.0/Library/bin")

reader = easyocr.Reader(['hi','en']) # this needs to run only once to load the model into memory

for image in images:
    result = reader.readtext(np.array(image), detail=0)
    print(result)