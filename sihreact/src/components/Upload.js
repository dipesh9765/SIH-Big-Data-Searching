import React from 'react'

export default function Upload() {
  return (
    <>
        {/* <form method='post' action="/" encType='multipart-form-data'>
            <input type="file"/>
            <button type='submit'>Submit</button>
        </form> */}
        <div className='Container'>
              <form method='post' action="/" encType='multipart-form-data'>
                  <input type="file"/>
                  <button type='submit'>Submit</button>
              </form>
        </div>
    </>
  )
}
