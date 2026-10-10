// Validation-only types and I/O adapters. The algorithms are extracted verbatim
// from the official SDK by build_dng_reference.py; this file supplies no weights.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <vector>
using real64=double; using uint32=uint32_t; using int32=int32_t;
#define DNG_REQUIRE(c,m) do {if(!(c)) throw std::runtime_error(m);} while(0)
#define DNG_ASSERT(c,m) DNG_REQUIRE(c,m)
inline void ThrowBadFormat(const char *s) {throw std::runtime_error(s);}
inline real64 Min_real64(real64 a,real64 b){return std::min(a,b);}
inline real64 Max_real64(real64 a,real64 b){return std::max(a,b);}
inline real64 Pin_real64(real64 lo,real64 a,real64 hi){return std::clamp(a,lo,hi);}
inline real64 Pin_real64(real64 a){return Pin_real64(0.,a,1.);}
struct dng_xy_coord {real64 x,y;dng_xy_coord(real64 a=0,real64 b=0):x(a),y(b){}};
struct dng_vector_3 {
  real64 v[3]; dng_vector_3(real64 x=0,real64 y=0,real64 z=0):v{x,y,z}{}
  real64 &operator[](int i){return v[i];} real64 operator[](int i)const{return v[i];}
  real64 MinEntry()const{return std::min({v[0],v[1],v[2]});}
};
inline dng_xy_coord D50_xy_coord(){return {.3457,.3585};}
dng_xy_coord XYZtoXY(const dng_vector_3 &a);
dng_vector_3 XYtoXYZ(const dng_xy_coord &a);
void LegacySetXY(const dng_xy_coord &,real64 &,real64 &);
class dng_temperature {
  real64 t,g;
public:
  explicit dng_temperature(const dng_xy_coord &xy){LegacySetXY(xy,t,g);}
  real64 Temperature()const{return t;} real64 Tint()const{return g;}
};
struct dng_point_real64 {real64 v,h;dng_point_real64(real64 y,real64 x):v(y),h(x){}};
inline dng_point_real64 operator-(const dng_point_real64 &a,const dng_point_real64 &b){return {a.v-b.v,a.h-b.h};}
struct dng_1d_function {virtual real64 Evaluate(real64)const=0;virtual ~dng_1d_function()=default;};
struct dng_urational {
  uint32 n,d;dng_urational(uint32 a=0,uint32 b=1):n(a),d(b){}
  real64 As_real64()const{return real64(n)/d;}
};
class dng_piecewise_linear {
public:
  std::vector<real64> X,Y;
  void Add(real64 x,real64 y){X.push_back(x);Y.push_back(y);}
  bool IsValid()const{
    if(X.size()<2 || X.size()!=Y.size())return false;
    for(size_t i=1;i<X.size();i++)if(!(X[i]>X[i-1]))return false;
    return true;
  }
  real64 Evaluate(real64 x)const;
};
class dng_illuminant_data {
  dng_xy_coord fDerivedWhite;
  dng_urational fMinLambda,fLambdaSpacing;
  std::vector<dng_urational> fSpectrum;
  void CalculateSpectrumXY();
public:
  const dng_xy_coord &WhiteXY()const{return fDerivedWhite;}
  void SetWhiteXY(dng_xy_coord a){fDerivedWhite=XYZtoXY(XYtoXYZ(a));}
  void SetSpectrum(dng_urational a,dng_urational b,std::vector<dng_urational> c){
    fMinLambda=a;fLambdaSpacing=b;fSpectrum=c;CalculateSpectrumXY();
  }
};
