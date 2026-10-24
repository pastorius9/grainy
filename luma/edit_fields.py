"""Shared numeric edit ranges for Grainy photo tools."""

EDIT_FIELDS={
    'exposure':('노출',-5,5),'contrast':('대비',-100,100),
    'highlights':('하이라이트',-100,100),'shadows':('그림자',-100,100),
    'whites':('흰색 계열',-200,200),'blacks':('검정 계열',-200,200),
    'temperature':('색온도',-100,100),'tint':('색조',-100,100),
    'saturation':('채도',-100,100),'vibrance':('생동감',-100,100),
    'clarity':('부분 대비',-100,100),'sharpen':('선명하게',0,150),
    'vignette':('비네팅',0,100),'grain':('그레인',0,100),
    'vignette_midpoint':('비네팅 중간점',0,100),'vignette_roundness':('비네팅 둥글기',-100,100),
    'vignette_feather':('비네팅 페더',0,100),'vignette_highlights':('비네팅 하이라이트 보호',0,100),
    'grain_size':('그레인 크기',0,100),'grain_roughness':('그레인 거칠기',0,100),
    'straighten':('수평 맞추기',-45,45),
    'monochrome':('흑백',0,1),
}
