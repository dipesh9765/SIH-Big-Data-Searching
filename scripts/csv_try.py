import csv
import time
import sys

sta = time.time();
filename = sys.argv[1]

# initializing the titles and rows list
fields = []
rows = []

# reading csv file
with open(filename, 'r') as csvfile:
	# creating a csv reader object
	csvreader = csv.reader(csvfile)
	
	# extracting field names through first row
	fields = next(csvreader)

	# extracting each data row one by one
	for row in csvreader:
		rows.append(row)

	# get total number of rows
	print("Total no. of rows: %d"%(csvreader.line_num))

# printing the field names
print('Field names are: ' + ' , '.join(field for field in fields))
end = time.time()


# printing first 5 rows
print('\nFirst 15000 rows are:\n')
for row in rows[:15000]:
	# parsing each column of a row
	for col in row:
		print("%10s"%col,end=" "),
	print('\n')
print(end-sta)
