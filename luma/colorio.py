"""LittleCMS float/16-bit conversions and export metadata."""
from functools import lru_cache
from pathlib import Path
from fractions import Fraction
import numpy as np
import imagecodecs
from PIL import Image

@lru_cache(maxsize=12)
def profile(name='sRGB'):
    if name=='Linear sRGB':return imagecodecs.cms_profile('linearrgb')
    if name in ('Linear ProPhoto','Luma Wide'):
        arguments={'gamma':1.0}
        if name=='Luma Wide':
            x=np.linspace(0,1,65536,dtype=np.float32)
            arguments={'transferfunction':np.where(x<=.04045,x/12.92,((x+.055)/1.055)**2.4)}
        return imagecodecs.cms_profile('rgb',whitepoint=(.3457,.3585),primaries=(.7347,.2653,.1596,.8404,.0366,.0001),**arguments)
    if name=='sRGB':return imagecodecs.cms_profile('srgb')
    if name=='Adobe RGB':return imagecodecs.cms_profile('adobergb')
    if name=='Display P3':
        x=np.linspace(0,1,4096,dtype=np.float32)
        transfer=np.where(x<=.04045,x/12.92,((x+.055)/1.055)**2.4)
        return imagecodecs.cms_profile('rgb',whitepoint=(.3127,.329),primaries=(.68,.32,.265,.69,.15,.06),transferfunction=transfer)
    if name=='ProPhoto RGB':return imagecodecs.cms_profile('rgb',whitepoint=(.3457,.3585),primaries=(.7347,.2653,.1596,.8404,.0366,.0001),gamma=1.8)
    if name=='Gray sRGB':
        # Gray with the sRGB tone curve: a gray value v displays like sRGB (v, v, v).
        x=np.linspace(0,1,65536,dtype=np.float32)
        return imagecodecs.cms_profile('gray',whitepoint=(.3127,.329),transferfunction=np.where(x<=.04045,x/12.92,((x+.055)/1.055)**2.4))
    result=Path(name).read_bytes();imagecodecs.cms_profile_validate(result)
    if imagecodecs.cms_info(result)['colorspace']!='rgb':raise ValueError('RGB ICC 프로파일을 선택하세요.')
    return result


def convert(rgb,source,destination,outdtype='float32'):
    array=np.ascontiguousarray(rgb)
    if array.dtype==np.float32 and np.dtype(outdtype)==np.dtype('float32') and array.ndim>=2 and array.shape[-1]==3 and isinstance(source,bytes) and isinstance(destination,bytes):
        from . import native_color
        if native_color.available() and max(len(source),len(destination))<=native_color.MAX_PROFILE_SIZE:
            return native_color.convert(array,source,destination)
    return imagecodecs.cms_transform(array,source,destination,
        colorspace='rgb',outcolorspace='rgb',outdtype=outdtype,intent=1)


def display_rgb(rgb,destination=None):
    if destination is None:return rgb
    return np.clip(convert(np.asarray(rgb,np.float32),profile('sRGB'),destination),0,1)


def export_exif(source,metadata=None,size=None,color_space='sRGB'):
    import piexif
    try:data=piexif.load(str(source))
    except Exception:
        try:
            with Image.open(source) as image:
                # PNG and WebP can carry EXIF although piexif's file reader
                # only understands some of their containers.
                image.getexif()
                data=piexif.load(image.info['exif'])
        except Exception:data={'0th':{},'Exif':{},'GPS':{},'1st':{},'thumbnail':None}
    data['1st']={};data['thumbnail']=None
    data['0th'][piexif.ImageIFD.Orientation]=1
    data['Exif'][piexif.ExifIFD.ColorSpace]=1 if color_space=='sRGB' else 65535
    data['Exif'].pop(piexif.ExifIFD.InteroperabilityTag,None)
    data['Interop']={}
    # Maker notes may contain offsets that become invalid after rewriting.
    data['Exif'].pop(piexif.ExifIFD.MakerNote,None)
    if size:
        data['Exif'][piexif.ExifIFD.PixelXDimension]=size[0];data['Exif'][piexif.ExifIFD.PixelYDimension]=size[1]
        data['0th'][piexif.ImageIFD.ImageWidth]=size[0];data['0th'][piexif.ImageIFD.ImageLength]=size[1]
    metadata=metadata or {}
    for key,tag in [('creator',piexif.ImageIFD.Artist),('copyright',piexif.ImageIFD.Copyright),('caption',piexif.ImageIFD.ImageDescription)]:
        if key in metadata:data['0th'][tag]=str(metadata[key]).encode('utf-8')
    if metadata.get('DateTimeOriginal'):
        stamp=metadata['DateTimeOriginal'].encode('ascii');data['Exif'][piexif.ExifIFD.DateTimeOriginal]=stamp
        data['Exif'][piexif.ExifIFD.DateTimeDigitized]=stamp
    def coordinate(value):
        value=abs(float(value));d=int(value);m=int((value-d)*60);s=Fraction((value-d-m/60)*3600).limit_denominator(100000)
        return ((d,1),(m,1),(s.numerator,s.denominator))
    for key,ref,value_tag,positive,negative in [('latitude',piexif.GPSIFD.GPSLatitudeRef,piexif.GPSIFD.GPSLatitude,b'N',b'S'),
            ('longitude',piexif.GPSIFD.GPSLongitudeRef,piexif.GPSIFD.GPSLongitude,b'E',b'W')]:
        if key in metadata:
            if metadata[key] is None or str(metadata[key]).strip()=='':
                data['GPS'].pop(ref,None);data['GPS'].pop(value_tag,None)
            else:
                data['GPS'][ref]=positive if float(metadata[key])>=0 else negative
                data['GPS'][value_tag]=coordinate(metadata[key])
    # Some readers expect a terminating next-IFD word even after the final
    # EXIF/GPS subdirectory. Padding does not change any stored offsets.
    return piexif.dump(data)+b'\0'*4


def append_tiff_exif(handle,exif):
    """Append metadata IFDs to our newly-created TIFF without re-encoding pixels.

    Existing strip offsets and encoded tags remain byte-for-byte intact. Only
    the root IFD pointer is redirected to a merged directory. The caller owns
    the exclusive-created file and removes it on any failure.
    """
    import struct
    handle.flush();handle.seek(0);header=handle.read(8)
    endian='<' if header[:2]==b'II' else '>'
    if struct.unpack(endian+'H',header[2:4])[0]!=42:
        raise ValueError('4GB 이상 BigTIFF의 EXIF 저장은 아직 지원하지 않습니다. 메타데이터 보존을 끄고 내보내세요.')
    root=struct.unpack(endian+'I',header[4:8])[0]
    handle.seek(root);count=struct.unpack(endian+'H',handle.read(2))[0]
    entries={}
    for _ in range(count):
        entry=handle.read(12);entries[struct.unpack(endian+'H',entry[:2])[0]]=entry
    if handle.read(4)!=b'\0'*4:raise ValueError('여러 페이지 TIFF에 촬영 정보를 추가할 수 없습니다.')
    original=Image.Exif();original.load(exif)
    metadata=Image.Exif();metadata.endian=endian
    # Do not copy a source TIFF's image layout, offsets, thumbnails or profiles.
    for tag in (269,270,271,272,274,282,283,296,306,315,33432,40091,40092,40093,40094,40095):
        if tag in original:metadata[tag]=original[tag]
    for tag in (34665,34853):
        values=original.get_ifd(tag)
        if values:metadata[tag]=values
    offset=handle.seek(0,2)
    if offset%2:handle.write(b'\0');offset+=1
    if offset>0xffffffff:raise ValueError('TIFF 메타데이터 주소 범위를 초과했습니다.')
    block=metadata.tobytes(offset=offset)[14:]
    handle.write(block)
    count=struct.unpack_from(endian+'H',block)[0]
    for i in range(count):
        entry=block[2+12*i:14+12*i]
        entries[struct.unpack(endian+'H',entry[:2])[0]]=entry
    root=handle.tell()
    if root%2:handle.write(b'\0');root+=1
    handle.write(struct.pack(endian+'H',len(entries)))
    for tag in sorted(entries):handle.write(entries[tag])
    handle.write(b'\0'*4)
    handle.seek(4);handle.write(struct.pack(endian+'I',root));handle.seek(0,2)


def iptc_bytes(metadata,keywords=''):
    """UTF-8 IPTC IIM records for exported captions and hierarchical keywords."""
    output=bytearray()
    def record(dataset,value):
        encoded=str(value).encode('utf-8')[:32700]
        if encoded:output.extend(bytes([0x1c,2,dataset])+len(encoded).to_bytes(2,'big')+encoded)
    output.extend(b'\x1c\x01\x5a\x00\x03\x1b%G')
    for key,number in [('title',5),('creator',80),('city',90),('location',92),('country',101),('copyright',116),('caption',120)]:record(number,metadata.get(key,''))
    for keyword in keywords.split(','):
        if keyword.strip():record(25,keyword.strip())
    return bytes(output)


def jpeg_iptc(path,metadata,keywords=''):
    iim=iptc_bytes(metadata,keywords)
    block=b'8BIM\x04\x04\x00\x00'+len(iim).to_bytes(4,'big')+iim+(b'\x00' if len(iim)%2 else b'')
    app13=b'Photoshop 3.0\x00'+block
    if len(app13)>65530:raise ValueError('IPTC 메타데이터가 너무 큽니다.')
    path=Path(path)
    with path.open('rb+') as file:
        data=file.read()
        if data[:2]!=b'\xff\xd8':raise ValueError('JPEG 파일이 아닙니다.')
        file.seek(0);file.write(data[:2]+b'\xff\xed'+(len(app13)+2).to_bytes(2,'big')+app13+data[2:]);file.truncate()


def xmp_metadata(metadata,keywords='',exif=None):
    import xml.etree.ElementTree as ET
    ns={'x':'adobe:ns:meta/','rdf':'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
        'dc':'http://purl.org/dc/elements/1.1/','exif':'http://ns.adobe.com/exif/1.0/',
        'photoshop':'http://ns.adobe.com/photoshop/1.0/','tiff':'http://ns.adobe.com/tiff/1.0/'}
    for k,v in ns.items():ET.register_namespace(k,v)
    root=ET.Element(f'{{{ns["x"]}}}xmpmeta');rdf=ET.SubElement(root,f'{{{ns["rdf"]}}}RDF')
    desc=ET.SubElement(rdf,f'{{{ns["rdf"]}}}Description')
    for key,prefix,tag in [('title','dc','title'),('caption','dc','description'),('creator','dc','creator'),('copyright','dc','rights')]:
        if metadata.get(key):
            element=ET.SubElement(desc,f'{{{ns[prefix]}}}{tag}')
            kind='Seq' if key=='creator' else 'Alt'
            bag=ET.SubElement(element,f'{{{ns["rdf"]}}}{kind}');li=ET.SubElement(bag,f'{{{ns["rdf"]}}}li')
            if kind=='Alt':li.set('{http://www.w3.org/XML/1998/namespace}lang','x-default')
            li.text=str(metadata[key])
    for key,prefix,tag in [('city','photoshop','City'),('country','photoshop','Country'),('location','photoshop','Location'),
        ('DateTimeOriginal','exif','DateTimeOriginal'),('latitude','exif','GPSLatitude'),('longitude','exif','GPSLongitude')]:
        if metadata.get(key) is not None and str(metadata[key]).strip():
            desc.set(f'{{{ns[prefix]}}}{tag}',str(metadata[key]))
    if keywords:
        bag=ET.SubElement(ET.SubElement(desc,f'{{{ns["dc"]}}}subject'),f'{{{ns["rdf"]}}}Bag')
        for k in keywords.split(','):
            if k.strip():ET.SubElement(bag,f'{{{ns["rdf"]}}}li').text=k.strip()
    if exif:
        import piexif
        data=piexif.load(exif)
        for table,prefix,entries in [('0th','tiff',[(271,'Make'),(272,'Model'),(315,'Artist'),(33432,'Copyright')]),
            ('Exif','exif',[(33434,'ExposureTime'),(33437,'FNumber'),(34855,'ISOSpeedRatings'),(37386,'FocalLength'),(36867,'DateTimeOriginal')])]:
            for ident,name in entries:
                value=data.get(table,{}).get(ident)
                if value is not None:
                    if isinstance(value,bytes):value=value.decode('utf-8','replace')
                    elif isinstance(value,tuple) and len(value)==2:value=f'{value[0]}/{value[1]}'
                    desc.set(f'{{{ns[prefix]}}}{name}',str(value))
    return ET.tostring(root,encoding='utf-8')
