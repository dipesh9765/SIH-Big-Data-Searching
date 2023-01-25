import * as React from 'react';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import CardMedia from '@mui/material/CardMedia';
import Typography from '@mui/material/Typography';
import { Button, CardActionArea, CardActions } from '@mui/material';

export default function MultiActionAreaCard() {
  return (
    <Card className="cardCls" sx={{ maxWidth: 150,maxHeight:180 }}>
      <CardActionArea>
        <CardMedia
          component="img"
          height="140"
          image="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAG0AAAB9CAMAAACf8B3/AAAAmVBMVEX////tHCQAAADsAABoaGjsABD0j5DsAAyKiorw8PDtFh/3sbL2pqeHh4f6+vruNz3tCxf2oKLwVlfBwcHye3waGhrl5eX5ysr0HSXziIn71tf1lZeNFhr+7e5+DxOpFBqampr4v8DybW6ioqKgAADuLDPydHX84+TxZmf+9fVQUFCvr6+LdHSEamuJAAB9AADGGB7vRUfIUVORCVXWAAADNElEQVRoge3ba5OaMBQG4OBpZAtUA6VFsbalXQR6Xff//7gqJhAgXKInzuyW91smkCcoJyCMhFR5WFydr0Q7N2iLh7tq+kd3k6bN3abpchftjW5+XMddtJXeFAl5f93RXbS312ta3O2aDoegaXAY2nQORZvM4WhTOSRtIoelTePQtMX3u2pTOERtAoepjXOo2iiHq41xyNoIh60Nc+jaIIevDXEGtAHOhNbPGdF6OTNaH2dIW/y8q/buJWsfeoOvrT72B18byl211azN2qz1PLn5ZUj7rera/TGk/d0qMPhkSPsMHW4HG2Paps3twDKoWU3uhBnVGtwZM6tJ3PKMGdYsCGXMtMY5jpnQiKyVXMIxa/P05ZTH7RIxj+chnzhggUtcWzQ2ZaiNF3oZUgC2J2nGM2uvR3OktLfv72ntqNxKoaXrOikDAKbqS2mz55x1N2Maa12KMvcAlPeB1+wJAerZO4FiLTk0JqTQws4uxZYvN+C3u9wUBrVv+hohEfRo9SqLqPHFVKURF9A1EtI+jXiAoa1W0gukPfRqJGC92uSzJFkfj0EYi/1yJml+kO+qHpJBQ9svd1WOzkTNhVN1UogEQCVta58KMd/LM6m1GOpLTrO8B7WyGUjNWjsPbx8L3ixPWUmzejKqOeuiHrGpSV8jmiYOIOlq1UzI+evB0OiBN72uZkEmtSVNpL3ej2ins0QMuGQKLRFTsSVttecpXNDQbDsNq/POchSap9LqJNM1xY4dTbTLzwFRSx2VZujYQttSaa4Rbdu64ghNrF5LiqdFQftqyuvNEudQLldAEYv4ulqReUF1/9HSaLVpay25ot6SIM/zYC3fejQ1xxa1mGGsXIyx5j1aQ2NVbZMdRdA6W9caUJaLIyOkHMGcFkdxUVnEb15N0bVGstZ9iVFtbzn302KO3UXzqwIxrmV+Wu98lebk0SXL7u86GkZVEv+wZvI9lXPkPZ6GZjG+7FDF5gyksNavM6dnvRrUDGbWMDWgPM7lyRNmyhEd0TpVUFw9Rns29FTtWbS2kVSspt72qd8+vI43K7M2a7M2a7M2a/+bpv3/gKGMatr/fRjKqIafWXvx2j9bk3NWYoEYXwAAAABJRU5ErkJggg=="
          alt="green iguana"
        />
        <CardContent>
          <Typography variant="body2" color="text.secondary">
            fileName1.pdf
          </Typography>
        </CardContent>
      </CardActionArea>
    </Card>
  );
}
