import mysql.connector
from mysql.connector import Error
import sys
import invertedindex
from dotenv import load_dotenv
load_dotenv()

try:
    connection = mysql.connector.connect(host=os.getenv('HOST'),
                                         database=os.getenv('DATABASE'),
                                         user=os.getenv('USER'),
                                         password= os.getenv('PASSWORD'))
    if connection.is_connected():
        db_Info = connection.get_server_info()
        print("Connected to MySQL Server version ", db_Info)
        cursor = connection.cursor()
        cursor.execute("select database();")
        record = cursor.fetchone()
        print("Database uploaded")
        invertedindex.func(sys.argv[1], sys.argv[2],connection)

except Error as e:
    print("Error while connecting to MySQL", e)
finally:
    if connection.is_connected():
        cursor.close()
        connection.close()
        print("MySQL connection is closed")