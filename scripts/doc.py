import docx
import sys

doc = docx.Document(sys.argv[1])

data = ""
fullText = []
for para in doc.paragraphs:
    fullText.append(para.text)
    data = '\n'.join(fullText)
 
print(data)