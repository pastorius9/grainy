# Colour conversion fixtures

`lcms-test-cmyk.icc` is Little CMS's `testbed/test1.icc`, obtained from
https://raw.githubusercontent.com/mm2/Little-CMS/master/testbed/test1.icc.
The repository MIT license is reproduced in `LittleCMS-LICENSE.txt`.
Its embedded copyright says **Not suitable for real use**: this synthetic profile
is used only to test CMYK channel scale and ICC conversion against Pillow/LCMS.
It is not an export profile or part of the application's runtime resources.

PNG interpretation follows https://www.w3.org/TR/png-3/.
TIFF photometric/alpha/orientation handling is necessary because
`tifffile.TiffPage.asarray()` returns stored samples, except JPEG YCbCr decoding.
