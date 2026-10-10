"""Targeted adjustment: drag up/down on the photo to change the tone curve or the
HSL mixer where the pressed pixel sits."""
from copy import deepcopy
import numpy as np
from .i18n import tr

HSL_CENTERS=np.array([0,1/12,1/6,1/3,.5,2/3,.75,5/6],np.float32)   # processing.mix_hsl
CURVE_PER_PIXEL=1/300    # curve output per dragged screen pixel
HSL_PER_PIXEL=.5         # HSL slider units per pixel for the dominant colour
NEAR_POINT=.04           # reuse a curve point this close to the pressed tone


def sample(image,x,y):
    """Mean RGB of the 3x3 pixels around the normalized point of the displayed photo."""
    h,w=image.shape[:2];px,py=min(w-1,int(x*w)),min(h-1,int(y*h))
    return np.asarray(image[max(0,py-1):py+2,max(0,px-1):px+2,:3],np.float32).reshape(-1,3).mean(axis=0)


def curve_input(points,rgb):
    """The curve input that produced the displayed tone: the curve is applied per channel, so its
    inverse at the displayed luminance (points with rising outputs; otherwise the luminance itself)."""
    y=float(np.clip(rgb@np.array([.2126,.7152,.0722],np.float32),0,1))
    p=np.asarray(points,np.float32)
    if len(p)<2 or np.any(np.diff(p[:,1])<=0):return y
    return float(np.clip(np.interp(y,p[:,1],p[:,0]),0,1))


def drag_curve(points,x,delta):
    """points with the point at input x (added if none is within NEAR_POINT) raised by delta, within
    0-1 like dragging it in the curve panel. The endpoints only move vertically."""
    p=[list(map(float,q)) for q in points]
    index=int(np.argmin([abs(q[0]-x) for q in p]))
    if abs(p[index][0]-x)>NEAR_POINT:
        p.append([x,float(np.interp(x,[q[0] for q in p],[q[1] for q in p]))]);p.sort()
        index=[q[0] for q in p].index(x)
    p[index][1]=float(np.clip(p[index][1]+delta,0,1))
    return p


def hsl_weights(rgb):
    """processing.mix_hsl's weight of each of the eight colours at this pixel's hue, largest = 1."""
    from .processing import rgb_to_hsl
    hue=float(rgb_to_hsl(np.asarray(rgb,np.float32).reshape(1,1,3))[0][0,0])
    weights=np.maximum(0,1-np.abs((hue-HSL_CENTERS+.5)%1-.5)/.125)**2
    return weights/max(float(weights.max()),1e-6)


def drag_hsl(groups,weights,column,delta):
    groups=deepcopy(groups)
    for index,weight in enumerate(weights):
        if weight>0:groups[index][column]=float(np.clip(groups[index][column]+delta*weight,-100,100))
    return groups


class TargetedAdjust:
    """Connects the photo view's 'adjust' tool to the curve and HSL mixer of the main window."""

    def __init__(self,window):
        self.w=window;self.kind=None;self.start=None;self.buttons={}
        view=window.view
        view.adjustStarted.connect(self.started);view.adjustDragged.connect(self.dragged)
        view.adjustFinished.connect(self.finished);view.toolModeChanged.connect(self.tool_changed)

    def toggle(self,kind,checked):
        w=self.w
        if not checked:
            if self.kind==kind:self.kind=None;w.view.set_tool('')
            return
        if w.source is None or kind=='curve' and w.settings.get('hdr'):
            self.buttons[kind].setChecked(False)
            if w.source is not None:w.statusBar().showMessage(tr('HDR 모드에서는 사진 위 커브 조정을 쓸 수 없습니다.'),5000)
            return
        if w.view.crop_mode:w.cancel_crop()
        w.wb_button.setChecked(False);w.view.sample_mode=False
        for other,button in self.buttons.items():
            if other!=kind:button.setChecked(False)
        w.view.set_tool('adjust');self.kind=kind
        w.statusBar().showMessage(tr('사진을 누른 채 위아래로 끌어 그 부분을 조정하세요. 버튼을 다시 누르면 끝납니다.'))
        w.view.setFocus()

    def tool_changed(self,mode):
        if mode!='adjust':
            self.kind=None;self.start=None
            for button in self.buttons.values():
                button.blockSignals(True);button.setChecked(False);button.blockSignals(False)

    def started(self,x,y):
        w=self.w
        if self.kind is None or w.view.on_screen is None:return
        w.commit()
        rgb=sample(w.view.on_screen,x,y)
        if self.kind=='curve':
            self.start=(deepcopy(w.settings['curve']),curve_input(w.settings['curve'],rgb))
        else:
            weights=hsl_weights(rgb)
            self.start=(deepcopy(w.settings['hsl']),weights)
            w.hsl_color.blockSignals(True);w.hsl_color.setCurrentIndex(int(np.argmax(weights)));w.hsl_color.blockSignals(False)
            w.update_hsl()

    def dragged(self,pixels):
        w=self.w
        if self.start is None:return
        if self.kind=='curve':
            points,x=self.start
            value=drag_curve(points,x,pixels*CURVE_PER_PIXEL)
            w.curve.set_points(value);w.set_setting('curve',value)
        else:
            groups,weights=self.start
            value=drag_hsl(groups,weights,w.hsl_target.currentIndex(),pixels*HSL_PER_PIXEL)
            w.set_setting('hsl',value);w.update_hsl()

    def finished(self):
        w=self.w
        if self.start is None:return
        label='사진에서 톤 커브 조정' if self.kind=='curve' else '사진에서 색상 혼합 조정'
        self.start=None
        w.commit(label);w.preview_timer.stop();w.render_live();w.refine_timer.start(25)
