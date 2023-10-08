import easyocr
import sys
reader = easyocr.Reader(['en'], model_storage_directory="./models") # this needs to run only once to load the model into memory
result = reader.readtext(sys.argv[1])
# print('\n'.join(result))
print(result)
sys.stdout.flush()