"""Precision-preserving bitmap decoding into the linear working colour space.

TIFF samples, orientation and alpha are interpreted explicitly; no implicit
Pillow RGB8 conversion is used for high-bit inputs. Source files are read-only.
"""
from pathlib import Path
import struct
import zlib

import imagecodecs
import numpy as np
from PIL import Image
from .colorio import profile, convert


def orient_pixels(pixels, orientation):
    operations = {
        2: lambda a: a[:, ::-1], 3: lambda a: a[::-1, ::-1],
        4: lambda a: a[::-1], 5: lambda a: a.swapaxes(0, 1),
        6: lambda a: np.rot90(a, -1),
        7: lambda a: a.swapaxes(0, 1)[::-1, ::-1],
        8: lambda a: np.rot90(a, 1),
    }
    return operations.get(int(orientation or 1), lambda a:a)(pixels)


def _normalise(pixels, bits=None):
    if pixels.dtype.kind == 'u':
        scale = float(2**(bits or pixels.dtype.itemsize*8)-1)
        if pixels.ndim == 3:
            # Row tiles on the pixel workers: identical values, ~5x faster on 12 MP.
            from .pixel_jobs import rows
            return rows(pixels.shape[0], pixels.shape[1], lambda first, stop: pixels[first:stop].astype(np.float32) / scale,
                        channels=pixels.shape[2])
        return pixels.astype(np.float32) / scale
    if pixels.dtype.kind == 'b':return pixels.astype(np.float32)
    if pixels.dtype.kind == 'f':
        if not np.isfinite(pixels).all():
            raise ValueError('이미지에 유효하지 않은 부동소수점 픽셀이 있습니다.')
        return pixels.astype(np.float32)
    raise ValueError(f'지원하지 않는 사진 픽셀 형식입니다: {pixels.dtype}')


_SRGB8_TABLE=None


def _srgb8_table():
    global _SRGB8_TABLE
    if _SRGB8_TABLE is None:
        from .engine import to_linear
        _SRGB8_TABLE=to_linear(_normalise(np.arange(256,dtype=np.uint8)))
    return _SRGB8_TABLE


def _srgb8_to_linear(pixels):
    """to_linear(_normalise(pixels)) for uint8 RGB via a lookup table; identical values, ~8x faster."""
    from .pixel_jobs import rows
    table=_srgb8_table()
    return rows(pixels.shape[0],pixels.shape[1],lambda start,stop:table[pixels[start:stop]])


def _encoded8_preview(pixels,orientation,max_size):
    """resize_float(orient(_srgb8_to_linear(pixels))) built one contiguous channel plane at a time.

    Each worker linearises, orients and resizes its own channel, so the full-size interleaved float
    frame and its per-channel copies are never made. PIL receives the same planes: identical values.
    """
    table=_srgb8_table();oriented=orient_pixels(pixels,orientation)
    return _plane_preview(lambda c:table[oriented[...,c]],oriented.shape[:2],max_size)


def _plane_preview(plane,shape,max_size,check_finite=False):
    """engine.resize_float over channel planes plane(c) of the oriented frame shape (h, w)."""
    from PIL import Image
    from .engine import _channel_pool
    h,w=shape;factor=max_size/max(w,h)
    size=(max(1,round(w*factor)),max(1,round(h*factor)))
    def resize(c):
        values=plane(c)
        if check_finite and not np.isfinite(values).all():raise ValueError('색상 변환 결과에 유효하지 않은 픽셀이 있습니다.')
        return np.asarray(Image.fromarray(values).resize(size,Image.Resampling.BILINEAR))
    return np.stack(list(_channel_pool().map(resize,range(3))),axis=-1),(w,h)


def _gray8_table(icc,working_space,info):
    """_linear's ICC gray transform for all 256 8-bit levels: a per-pixel colour transform, so
    table[pixels] equals transforming the whole frame (tests compare both), at a fraction of the work."""
    from .colorio import profile
    destination=profile('Linear ProPhoto' if working_space=='ProPhoto' else 'Linear sRGB')
    try:
        details=imagecodecs.cms_info(icc)
        if details['colorspace']!='gray':raise ValueError('픽셀 채널과 ICC 프로파일의 색공간이 다릅니다.')
        info['input_profile']=details.get('description') or 'GRAY'
        levels=_normalise(np.arange(256,dtype=np.uint8))[None,:]
        table=imagecodecs.cms_transform(np.ascontiguousarray(levels),icc,destination,
            colorspace='gray',outcolorspace='rgb',outdtype='float32',intent=1)[0]
    except (ValueError,RuntimeError) as error:
        raise ValueError('내장 ICC 프로파일을 작업 색공간으로 변환하지 못했습니다.') from error
    table=np.ascontiguousarray(table,dtype=np.float32)
    if not np.isfinite(table).all():raise ValueError('색상 변환 결과에 유효하지 않은 픽셀이 있습니다.')
    return table


def _icc8_preview(pixels,icc,working_space,info,orientation,max_size):
    """_finish's ICC branch for 8-bit RGB previews: convert8_planar planes, oriented and resized per channel."""
    from .colorio import profile
    from .native_color import convert8_planar
    destination=profile('Linear ProPhoto' if working_space=='ProPhoto' else 'Linear sRGB')
    try:
        details=imagecodecs.cms_info(icc)
        if details['colorspace']!='rgb':raise ValueError('픽셀 채널과 ICC 프로파일의 색공간이 다릅니다.')
        info['input_profile']=details.get('description') or 'RGB'
        planes=convert8_planar(pixels,icc,destination)
    except (ValueError,RuntimeError) as error:
        raise ValueError('내장 ICC 프로파일을 작업 색공간으로 변환하지 못했습니다.') from error
    turned=int(orientation or 1) in (5,6,7,8)
    shape=planes.shape[1:][::-1] if turned else planes.shape[1:]
    return _plane_preview(lambda c:orient_pixels(planes[c],orientation),shape,max_size,check_finite=True)


def _linear(samples, space, icc, working_space, info, *, linear=False, encoded8=False):
    from .engine import to_linear
    destination=profile('Linear ProPhoto' if working_space=='ProPhoto' else 'Linear sRGB')
    if icc:
        try:
            details=imagecodecs.cms_info(icc)
            if details['colorspace']!=space:
                raise ValueError('픽셀 채널과 ICC 프로파일의 색공간이 다릅니다.')
            info['input_profile']=details.get('description') or space.upper()
            # LittleCMS float CMYK uses percentages; float RGB/gray use 0..1.
            data=samples*100 if space=='cmyk' else samples
            if space=='gray':data=data[...,0]
            if space=='rgb':return convert(data,icc,destination)
            return imagecodecs.cms_transform(np.ascontiguousarray(data),icc,destination,
                colorspace=space,outcolorspace='rgb',outdtype='float32',intent=1)
        except (ValueError,RuntimeError) as error:
            raise ValueError('내장 ICC 프로파일을 작업 색공간으로 변환하지 못했습니다.') from error
    if space=='cmyk':
        samples=(1-samples[...,:3])*(1-samples[...,3,None])
        info['input_profile']='태그 없는 CMYK · 근사 RGB 변환'
    elif space=='gray':samples=np.repeat(samples,3,axis=-1)
    elif space=='lab':
        return imagecodecs.cms_transform(np.ascontiguousarray(samples),imagecodecs.cms_profile('lab4'),destination,
            colorspace='lab',outcolorspace='rgb',outdtype='float32',intent=1)
    info.setdefault('input_profile','태그 없는 선형 RGB' if linear else '태그 없는 sRGB')
    rgb=_srgb8_to_linear(samples) if encoded8 else samples if linear else to_linear(samples)
    return convert(rgb,profile('Linear sRGB'),destination) if working_space=='ProPhoto' else rgb


def _finish(pixels, info, space, icc, working_space, max_size, *, alpha=None,
            associated=False, linear=False, orientation=1,full_size=None,encoded8=False,rgb8=None,gray8=None):
    from .engine import resize_float
    preview=max_size and alpha is None and not associated and max(pixels.shape[:2])>max_size
    gray_table=_gray8_table(icc,working_space,info) if gray8 is not None else None
    if preview and (encoded8 and working_space!='ProPhoto' or rgb8 is not None and icc or gray_table is not None):
        if gray_table is not None:
            oriented=orient_pixels(gray8,orientation)
            rgb,(width,height)=_plane_preview(lambda c:gray_table[:,c][oriented],oriented.shape[:2],max_size)
        elif encoded8:
            # Untagged camera JPEG preview: the LUT values are finite, so no finiteness scan is needed.
            info.setdefault('input_profile','태그 없는 sRGB')
            rgb,(width,height)=_encoded8_preview(pixels,orientation,max_size)
        else:rgb,(width,height)=_icc8_preview(rgb8,icc,working_space,info,orientation,max_size)
        info.update(width=width,height=height,source_orientation=int(orientation or 1))
        if full_size:
            width,height=full_size
            if int(orientation or 1) in (5,6,7,8):width,height=height,width
            info.update(width=width,height=height)
        return rgb,info
    if associated and alpha is not None:
        pixels=np.divide(pixels,alpha[...,None],out=np.zeros_like(pixels),where=alpha[...,None]>0)
    if gray_table is not None:
        from .pixel_jobs import rows
        rgb=rows(gray8.shape[0],gray8.shape[1],lambda first,stop:gray_table[gray8[first:stop]])
    else:rgb=_linear(pixels,space,icc,working_space,info,linear=linear,encoded8=encoded8)
    if alpha is not None:
        alpha=np.clip(alpha,0,1)[...,None]
        # A photographic canvas has an opaque white background. Composite in
        # linear light so transparent edges do not acquire a dark fringe.
        rgb=rgb*alpha+(1-alpha)
        info['alpha']='흰색 배경 · 선형광 합성'
    rgb=np.ascontiguousarray(orient_pixels(rgb,orientation),dtype=np.float32)
    from .pixel_jobs import all_finite
    if not all_finite(rgb):raise ValueError('색상 변환 결과에 유효하지 않은 픽셀이 있습니다.')
    info.update(width=rgb.shape[1],height=rgb.shape[0],source_orientation=int(orientation or 1))
    if full_size:
        width,height=full_size
        if int(orientation or 1) in (5,6,7,8):width,height=height,width
        info.update(width=width,height=height)
    return (resize_float(rgb,max_size) if max_size else rgb),info


def _rationals(value, count):
    values=np.asarray(value,dtype=np.float64)
    if values.size==count*2:
        values=values.reshape(count,2)
        if np.any(values[:,1]==0):raise ValueError('TIFF 색상 정보의 분모가 0입니다.')
        values=values[:,0]/values[:,1]
    if values.size!=count or not np.isfinite(values).all():raise ValueError('TIFF 색상 정보가 올바르지 않습니다.')
    return values.ravel()


def _tiff_profile(page,space):
    tag=page.tags.get('InterColorProfile')
    if tag:return tag.value
    if space not in ('rgb','gray'):return None
    white=page.tags.get('WhitePoint');primaries=page.tags.get('PrimaryChromaticities')
    transfer=page.tags.get('TransferFunction')
    if not any((white,primaries,transfer)):return None
    args={'whitepoint':_rationals(white.value,2) if white else (.3127,.329)}
    if space=='rgb':
        args['primaries']=_rationals(primaries.value,6) if primaries else (.64,.33,.30,.60,.15,.06)
    if transfer:
        curves=np.asarray(transfer.value,dtype=np.float32)/65535
        if space=='rgb' and curves.size==3*2**page.bitspersample:
            curves=curves.reshape(3,-1)
        args['transferfunction']=curves
    else:
        from .engine import to_linear
        args['transferfunction']=to_linear(np.linspace(0,1,65536,dtype=np.float32))
    return imagecodecs.cms_profile(space,**args)


def read_tiff(path, working_space='sRGB', max_size=None):
    import tifffile
    with tifffile.TiffFile(path) as file:
        page=file.pages[0]
        pixels=page.asarray()
        if pixels.ndim==3 and page.planarconfig==2:pixels=np.moveaxis(pixels,0,-1)
        if pixels.ndim==2:pixels=pixels[...,None]
        if pixels.ndim!=3 or not pixels.size:
            raise ValueError('2차원 사진 TIFF 페이지를 읽을 수 없습니다.')
        bits=page.bitspersample
        if isinstance(bits,tuple):
            if len(set(bits))!=1:raise ValueError('채널별 비트 수가 다른 TIFF는 지원하지 않습니다.')
            bits=bits[0]
        photometric=int(page.photometric)
        floating=pixels.dtype.kind=='f'
        info={'decoder':f'TIFF {bits}-bit'+(' float' if floating else ''),'bit_depth':bits}
        if len(file.pages)>1:info['pages']=len(file.pages)
        samples=_normalise(pixels,bits)
        alpha=None;associated=False
        base_channels=samples.shape[-1]-len(page.extrasamples)
        for index,kind in enumerate(page.extrasamples):
            if kind in (1,2):
                alpha=samples[...,base_channels+index].copy();associated=kind==1;break
        if associated and photometric not in (1,2):
            raise ValueError('이 TIFF 색상 형식의 미리 곱한 투명도는 지원하지 않습니다. RGB TIFF로 변환해 주세요.')
        if photometric in (0,1):
            space='gray';channels=1
            if photometric==0:samples[...,:1]=1-samples[...,:1]
        elif photometric==2:space='rgb';channels=3
        elif photometric==3:
            if page.colormap is None:raise ValueError('TIFF 색상표가 없습니다.')
            samples=np.moveaxis(page.colormap[:,pixels[...,0]],0,-1).astype(np.float32)/65535
            space='rgb';channels=3;info['bit_depth']=16
        elif photometric==5:
            if page.tags.get('InkSet') and int(page.tags['InkSet'].value)!=1:
                raise ValueError('CMYK 외의 다중 잉크 TIFF는 지원하지 않습니다.')
            space='cmyk';channels=4
        elif photometric==6 and page.compression in (6,7,33007,34892):
            # tifffile/imagecodecs decode JPEG YCbCr strips to RGB already.
            space='rgb';channels=3
        elif photometric==6:
            coefficients=page.tags.get('YCbCrCoefficients')
            kr,kg,kb=_rationals(coefficients.value,3) if coefficients else (.299,.587,.114)
            if kg<=0:raise ValueError('TIFF YCbCr 색상 계수가 올바르지 않습니다.')
            reference=page.tags.get('ReferenceBlackWhite')
            ref=_rationals(reference.value,6) if reference else (0,2**bits-1,2**(bits-1),2**bits-1,2**(bits-1),2**bits-1)
            maximum=float(2**bits-1);raw=samples*maximum
            y=(raw[...,0]-ref[0])/max(1,ref[1]-ref[0])
            cb=(raw[...,1]-ref[2])/max(1,ref[3]-ref[2])*.5
            cr=(raw[...,2]-ref[4])/max(1,ref[5]-ref[4])*.5
            r=y+cr*(2-2*kr);b=y+cb*(2-2*kb);g=(y-kr*r-kb*b)/kg
            samples=np.stack([r,g,b],-1);space='rgb';channels=3
        elif photometric in (8,9):
            if floating:lab=samples[...,:3].copy()
            else:
                lab=samples[...,:3].copy();lab[...,0]*=100
                if photometric==8:
                    # TIFF CIELAB chroma uses signed two's-complement samples.
                    chroma=pixels[...,1:3].astype(np.int64)
                    chroma=np.where(chroma>=2**(bits-1),chroma-2**bits,chroma)
                    lab[...,1:3]=chroma/(2**max(0,bits-8))
                else:lab[...,1:3]=lab[...,1:3]*255-128
            samples=lab;space='lab';channels=3
        else:raise ValueError(f'지원하지 않는 TIFF 색상 형식입니다: {page.photometric.name}')
        if samples.shape[-1]<channels:raise ValueError('TIFF 색상 채널이 부족합니다.')
        icc=_tiff_profile(page,space)
        tag=page.tags.get('Orientation');orientation=tag.value if tag else 1
        return _finish(samples[...,:channels],info,space,icc,working_space,max_size,
            alpha=alpha,associated=associated,linear=floating and space!='lab',orientation=orientation)


def _png_profile(info, space):
    if info.get('icc_profile'):return info['icc_profile']
    if 'srgb' in info:return profile('sRGB') if space=='rgb' else None
    gamma=info.get('gamma');chroma=info.get('chromaticity')
    if gamma is None and chroma is None:return None
    if gamma is not None and (not np.isfinite(gamma) or gamma<=0):
        raise ValueError('PNG 감마 정보가 올바르지 않습니다.')
    args={}
    if gamma is not None:args['gamma']=1/gamma
    else:
        from .engine import to_linear
        args['transferfunction']=to_linear(np.linspace(0,1,65536,dtype=np.float32))
    if chroma is not None:
        args['whitepoint']=chroma[:2]
        if space=='rgb':args['primaries']=chroma[2:]
    else:
        args['whitepoint']=(.3127,.329)
        if space=='rgb':args['primaries']=(.64,.33,.30,.60,.15,.06)
    return imagecodecs.cms_profile(space,**args)


def read_png(path,working_space='sRGB',max_size=None):
    data=Path(path).read_bytes()
    with Image.open(path) as header:
        info=dict(header.info);orientation=header.getexif().get(274,1)
    # Pillow does not currently expose cICP, so read color chunks explicitly.
    chunks={};offset=8
    while offset+12<=len(data):
        size=struct.unpack_from('>I',data,offset)[0];kind=data[offset+4:offset+8]
        end=offset+12+size
        if end>len(data):raise ValueError('PNG 청크가 잘렸습니다.')
        if kind in (b'cICP',b'iCCP'):chunks[kind]=data[offset+8:end-4]
        if kind==b'IDAT':break
        offset=end
    cicp=chunks.get(b'cICP')
    if cicp is not None:
        if tuple(cicp) not in ((1,13,0,1),(12,13,0,1)):
            raise ValueError('이 PNG의 cICP HDR/영상 색공간은 아직 지원하지 않습니다.')
        info={'srgb':0} if cicp[0]==1 else {'icc_profile':profile('Display P3')}
    elif b'iCCP' in chunks and not info.get('icc_profile'):
        raise ValueError('PNG의 내장 ICC 프로파일을 읽지 못했습니다.')
    pixels=imagecodecs.png_decode(data)
    if pixels.ndim==2:pixels=pixels[...,None]
    samples=_normalise(pixels)
    channels=samples.shape[-1];space='gray' if channels<=2 else 'rgb'
    count=1 if space=='gray' else 3
    alpha=samples[...,count] if channels==count+1 else None
    icc=_png_profile(info,space)
    details={'decoder':f'PNG {data[24]}-bit','bit_depth':int(data[24])}
    return _finish(samples[...,:count],details,space,icc,working_space,max_size,
        alpha=alpha,orientation=orientation)


def read_bitmap(path,working_space='sRGB',max_size=None):
    path=Path(path)
    if path.suffix.lower() in ('.tif','.tiff'):return read_tiff(path,working_space,max_size)
    if path.suffix.lower()=='.png':return read_png(path,working_space,max_size)
    with Image.open(path) as image:
        icc=image.info.get('icc_profile');orientation=image.getexif().get(274,1)
        full_size=image.size
        # JPEG's decoder can perform a reduced IDCT for previews. Keep at
        # least twice the requested size, then resize in linear light below.
        if max_size and image.format=='JPEG':image.draft(image.mode,(max_size*2,max_size*2))
        alpha=None
        if image.mode=='CMYK':space='cmyk';pixels=np.asarray(image)
        elif image.mode=='LAB':
            space='lab';pixels=np.asarray(image).astype(np.float32)
            pixels[...,0]*=100/255
            # Pillow's array/raw LAB representation stores signed chroma as
            # two's complement, unlike its offset getpixel() representation.
            pixels[...,1:]=np.where(pixels[...,1:]>=128,pixels[...,1:]-256,pixels[...,1:])
        elif image.mode in ('L','LA','1','I;16','I;16B','I;16L'):
            space='gray';pixels=np.asarray(image)
            if pixels.ndim==2:pixels=pixels[...,None]
        else:
            space='rgb'
            image=image.convert('RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB')
            pixels=np.asarray(image)
        # Untagged opaque 8-bit RGB (typical camera JPEG): linearise through a 256-entry table.
        encoded8=space=='rgb' and not icc and pixels.dtype==np.uint8 and pixels.shape[-1]==3
        # Tagged 8-bit RGB previews convert straight from the 8-bit samples (_icc8_preview);
        # tagged 8-bit gray goes through a 256-level table (_gray8_table).
        rgb8=pixels if space=='rgb' and icc and pixels.dtype==np.uint8 and pixels.shape[-1]==3 and max_size else None
        gray8=pixels[...,0] if space=='gray' and icc and pixels.dtype==np.uint8 and pixels.shape[-1]==1 else None
        samples=pixels if space=='lab' or encoded8 or gray8 is not None or rgb8 is not None and max(pixels.shape[:2])>max_size else _normalise(pixels)
        count={'gray':1,'rgb':3,'cmyk':4,'lab':3}[space]
        if samples.shape[-1]==count+1:alpha=samples[...,count]
        bits=8 if space=='lab' else pixels.dtype.itemsize*8
        info={'decoder':f'{image.format or path.suffix[1:].upper()} · {bits}-bit','bit_depth':bits}
        return _finish(samples[...,:count],info,space,icc,working_space,max_size,
            alpha=alpha,orientation=orientation,full_size=full_size,encoded8=encoded8,rgb8=rgb8,gray8=gray8)


def png_chunk(kind, payload):
    return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)


def write_png16(handle, pixels, icc, exif=None, xmp=None):
    encoded=imagecodecs.png_encode(np.ascontiguousarray(pixels,dtype=np.uint16),level=6)
    # Keep the encoder's 16-bit IDAT untouched; insert standard ancillary chunks
    # after IHDR, before the first image data chunk.
    end=8+12+struct.unpack('>I',encoded[8:12])[0]
    handle.write(encoded[:end])
    handle.write(png_chunk(b'iCCP',b'Luma color\0\0'+zlib.compress(icc)))
    if exif:handle.write(png_chunk(b'eXIf',exif[6:] if exif.startswith(b'Exif\0\0') else exif))
    if xmp:handle.write(png_chunk(b'iTXt',b'XML:com.adobe.xmp\0\0\0\0\0'+xmp))
    handle.write(encoded[end:])
