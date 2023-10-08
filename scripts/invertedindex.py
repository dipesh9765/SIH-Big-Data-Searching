def func(data, id, connection):
   
    # file = open('./scripts/document1.txt',mode="r", encoding='utf8')
    read = data
    # print(read)
    # file.seek(0)
    print(read)

    # to obtain the
    # number of lines
    # in file

    # line = 1
    # for word in read:
    #     if word == '\n':
    #         line += 1
    # print("Number of lines in file is: ", line)

    # create a list to
    # store each line as
    # an element of list
    array = [read]
    # for i in range(line):
    # 	array.append(read)

    # print(array)

    punc = '''!()-[]{};:'"\, <>./?@#$%^&*_~'''
    for ele in read:  
        if ele in punc:  
            read = read.replace(ele, " ")  
            
    read
    
    # to maintain uniformity
    read=read.lower()                    
    read


    from nltk.tokenize import word_tokenize
    import nltk
    from nltk.corpus import stopwords
    nltk.download('stopwords')
    nltk.download('punkt')
    
    for i in range(1):
        # this will convert
        # the word into tokens
        text_tokens = word_tokenize(read)
    
    tokens_without_sw = [
        word for word in text_tokens if not word in stopwords.words()]
    
    # print(tokens_without_sw)

    from nltk.stem import PorterStemmer
    from nltk.tokenize import word_tokenize

    ps = PorterStemmer()

    stemmedword=[]
    for word in tokens_without_sw:
        stemmedword.append(ps.stem(word))
            
    # for word in tokens_without_sw:
    #     word = ps.stem(word)

    print(stemmedword)
    # tokens_without_sw = [
    #     word for word in text_tokens if not word in ps.stem()]

    dict = {}
    
    for i in range(1):
        check = array[0].lower()
        for item in tokens_without_sw:
            mySql_insert_query = """INSERT INTO inverted_index (word_name,word_id) VALUES (%s,%s) ON DUPLICATE KEY UPDATE word_id = if(JSON_CONTAINS(word_id, %s, '$')=1,word_id, JSON_ARRAY_APPEND(word_id, '$', %s)); ;
            """
            idts = int(id)
            val = (item,id, id, idts)
            cursor = connection.cursor()
            cursor.execute(mySql_insert_query,val)
            connection.commit()
            print(cursor.rowcount, "Record inserted successfully into Laptop table")
            cursor.close()
            # if item in check:
            #     if item not in dict:
            #         dict[item] = []
    
            #     if item in dict:
            #         if id not in dict[item]:
            #             dict[item].append(id)

    
    # print(dict)