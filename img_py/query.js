
var state = "SELECT * FROM user_details WHERE last_name = ? AND gender= ?";
Pool.query(state, [paul, female], (err, result) => {
    if (err) throw err;
    console.log(result);
})