const express = require("express");
const multer = require("multer");
const ejs = require("ejs");
const path = require("path");
const { createPool } = require("mysql");
const { spawn } = require("child_process");
const bodyParser = require('body-parser');
const { dirname } = require("path");

const pool = createPool({
  Host: "localhost",
  Port: 3360,
  user: "root",
  password: "Anurag@123",
  database: "sys",
});

const app = express();

// pool.query("select * from files", (err, result, fields) => {
//   if (err) {
//     return console.log(err);
//   }
//   return console.log(result);
// });
module.exports = pool;

app.use(express.static(__dirname +"/public"));
app.use(bodyParser.urlencoded({ extended: true }));
app.set("view engine", "ejs");

const storage = multer.diskStorage({
  destination: (req, file, cb) => {
    cb(null, "public/uploads");
  },
  filename: (req, file, cb) => {
    cb(null, file.originalname);
  },
});

const upload = multer({ storage });

app.get("/", (req, res) => {
  res.render("index", { result: " ", imgsrc: " " });
});

app.post("/upload", upload.single("file"), (req, res) => {
  const fileName = "./uploads/" + req.file.originalname;
  const fileurl = "./public/uploads/" + req.file.originalname;

  var imgsrc = "http://127.0.0.1:3000/public/uploads/" + req.file.filename;
  var insertData = "INSERT INTO FILES(file_src)VALUES(?)";
  // pool.query(insertData, [imgsrc], (err, result) => {
  //   if (err) throw err;
  //   console.log("file uploaded");
  // });

  const extension = path.extname(fileName);
  // console.log(extension);
  var dataToSend;
  var python;
  if (extension === ".jpeg" || extension === ".jpg" || extension === ".png") {
    // python = spawn("python", ["./scripts/text.py", fileurl]); //for images of format jpg, jpeg, png
    python = spawn("python", ["./scripts/cmdimg.py", fileurl]); //for images of format jpg, jpeg, png
  } else if (extension === ".pdf") {
    python = spawn("python", ["./scripts/pdftoword.py", fileurl]); //for pdf
  } else if (extension === ".docx") {
    python = spawn("python", ["./scripts/doc.py", fileurl]); //for docx
  }
  else if (extension === ".csv") {
    python = spawn("python", ["./scripts/csv_try.py", fileurl]); //for docx
  }
  python.stdout.on("data", function (data) {
    dataToSend = data.toString();
    console.log(dataToSend);
  });

  python.on("close", (code) => {
    // console.log(dataToSend);
    res.render("index", { result: dataToSend, imgsrc: fileName });
    var state = "INSERT INTO FILES(file_src,FILE_TEXT)VALUES(?,?)";
    pool.query(state, [fileName, req.file.originalname], (err, result) => {
      if (err) throw err;
      const id = result.insertId;
      python = spawn("python", ["./scripts/database.py", dataToSend, id]);
      python.stdout.on("data", function (data) {
        indexeddata = data.toString();
        console.log(indexeddata);
      });

      console.log("uploaded");
    });
  });
});



app.get("/upload", (req, res)=>{
  res.render("upload");
});

app.post("/", (req, res)=>{
  const wordtosearch = req.body.name[0];
  console.log(wordtosearch)
  var arr = [];
  const state = "SELECT word_id FROM inverted_index WHERE word_name = ?"
  pool.query(state, [wordtosearch], (err, result) => {
    try {
      const result1 = Object.values(JSON.parse(JSON.stringify(result)));
    const result2 = result1[0].word_id.replace(/[\])}[{(]/g, '').split(',');
    const state1 = "SELECT file_text FROM files WHERE id=?";
    var i=0;
    result2.forEach((v) => pool.query(state1,[v],(err, result)=>{
      if(err) throw err;
      const result_name = Object.values(JSON.parse(JSON.stringify(result)));
        if (result_name== []){
        console.log(result_name[0].file_text);
        arr.push(result_name[0].file_text);
        i=i+1;
      }
      // if(i==)
    }));
    // console.log(arr);
    console.log("---------------------");
    res.render("index");
  } catch (error) {
      console.log(error);
    }
    

    // .replace(/[\])}[{(]/g, '').split(',')
    
  });
  
  // res.render("search",{ result: arraydum });
});

const server = app.listen(3000, () => {
  console.log("Server started on port 3000");
});

server.timeout = 300000;
