import React from 'react'
import Form from 'react-bootstrap/Form';
import Button from 'react-bootstrap/Button';
import PictureAsPdfIcon from '@mui/icons-material/PictureAsPdf';
import Card from "./Card"

export default function Search() {
  return (
    <div className='Findbox'>
        <Form className="d-flex searchbox">
            <Form.Control
              type="search"
              placeholder="Search"
              className="me-2"
              aria-label="Search"
            />
            <Button variant="outline-success">Search</Button>
        </Form>
        <div className='Result'>
              {/* {filesToShow.map(item=>{
                <Card key={item.name} format={item.format} name={item.name}/>
              })} */}
              {/* <Card />
              <Card />
              <Card />
              <Card />
              <Card />
              <Card /> */}


        </div>
    </div>
  )
}
