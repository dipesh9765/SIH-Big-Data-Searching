import pandas as pd
import sys

workbook = pd.read_excel(sys.argv[1])
print(workbook.head())