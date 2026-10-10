"""Offline GPS view and local library interoperability."""
from .i18n import tr
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import numpy as np
from PySide6.QtCore import Qt,QPointF,QRectF,QTimer,QUrl
from PySide6.QtGui import QColor,QPen,QPainterPath
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QGraphicsView,
    QGraphicsScene,QFileDialog,QInputDialog,QMessageBox,QSlider,QDoubleSpinBox)



def lightroom_records(path):
    with sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required={'AgLibraryFile','AgLibraryFolder','AgLibraryRootFolder'}
        if not required<=tables:raise ValueError('이 Lightroom 카탈로그 구조는 지원하지 않습니다.')
        rows=db.execute('''SELECT f.id_local,f.baseName,f.extension,d.pathFromRoot,r.absolutePath
            FROM AgLibraryFile f JOIN AgLibraryFolder d ON f.folder=d.id_local
            JOIN AgLibraryRootFolder r ON d.rootFolder=r.id_local''').fetchall()
        ratings={}
        if 'Adobe_images' in tables:
            cols={r[1] for r in db.execute('PRAGMA table_info(Adobe_images)')}
            wanted=[k for k in ('rootFile','rating','pick','colorLabels','masterImage') if k in cols]
            if 'rootFile' in wanted:
                for row in db.execute('SELECT '+','.join(wanted)+' FROM Adobe_images'):
                    data=dict(row)
                    if data.get('masterImage') is None:ratings[row['rootFile']]=data
        result=[]
        for row in rows:
            name=row['baseName'];extension=row['extension'] or ''
            if extension and not name.lower().endswith('.'+extension.lower()):name+='.'+extension
            p=Path(row['absolutePath'])/(row['pathFromRoot'] or '')/name
            if not p.is_absolute():continue
            data=ratings.get(row['id_local'],{})
            result.append({'path':str(p),'rating':max(0,min(5,int(data.get('rating') or 0))),
                'flag':max(-1,min(1,int(data.get('pick') or 0)))})
        return result


class MapView(QGraphicsView):
    def __init__(self,photos,activate,parent=None):
        super().__init__(parent);self.setScene(QGraphicsScene(self));self.activate=activate
        self.setBackgroundBrush(QColor('#121b26'));self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        data=Path(__file__).resolve().parents[1]/'assets'/'world.geojson'
        if data.exists():
            for feature in json.loads(data.read_text(encoding='utf-8'))['features']:
                geometry=feature['geometry'];polygons=geometry['coordinates']
                if geometry['type']=='Polygon':polygons=[polygons]
                for polygon in polygons:
                    path=QPainterPath()
                    for ring in polygon:
                        path.moveTo(ring[0][0],-ring[0][1])
                        for lon,lat in ring[1:]:path.lineTo(lon,-lat)
                        path.closeSubpath()
                    self.scene().addPath(path,QPen(QColor('#536476'),.2),QColor('#283b48'))
        self.points=[]
        for p in photos:
            info={**p['info'],**p['user_metadata']}
            try:lat,lon=float(info['latitude']),float(info['longitude'])
            except (KeyError,ValueError,TypeError):continue
            if not -90<=lat<=90 or not -180<=lon<=180:continue
            marker=self.scene().addEllipse(-3,-3,6,6,QPen(QColor('#f7e3bd'),1),QColor('#e5a853'))
            marker.setFlag(marker.GraphicsItemFlag.ItemIgnoresTransformations)
            marker.setPos(lon,-lat);marker.setData(0,p['id']);marker.setZValue(5)
            marker.setToolTip(f'{p["name"]}\n{lat:.6f}, {lon:.6f}');self.points.append(marker)
        self.setSceneRect(-180,-90,360,180);QTimer.singleShot(0,lambda:self.fitInView(self.sceneRect(),Qt.AspectRatioMode.KeepAspectRatio))

    def wheelEvent(self,event):
        factor=1.3 if event.angleDelta().y()>0 else 1/1.3
        if .5<self.transform().m11()*factor<2000:self.scale(factor,factor)

    def mouseDoubleClickEvent(self,event):
        for item in self.items(event.position().toPoint()):
            if item.data(0) is not None:self.activate(item.data(0));return
        super().mouseDoubleClickEvent(event)


class ExtraTools:
    def __init__(self,w):
        self.w=w;self.dialogs=[];self.watch_paths=w.catalog.preference('watch_folders',[])
        menu=w.menuBar().addMenu(tr('추가 도구'))
        from .import_copy import dialog as copy_import
        for title,fn in [('사진 복사해서 가져오기 (카드·다른 폴더)…',lambda:copy_import(w)),('GPS 지도',self.map),
            ('현재 폴더 자동 가져오기',self.watch),('자동 가져오기 해제',self.unwatch),
            ('Lightroom 사진·컬렉션 가져오기',self.import_lightroom),
            ('최근 Lightroom 가져오기 결과',self.lightroom_report)]:
            w.manager.add(menu,title,fn)
        self.timer=QTimer(w);self.timer.setInterval(20000);self.timer.timeout.connect(self.watch_scan)
        if self.watch_paths:self.timer.start()

    def watch(self):
        folder=self.w.folder_filter or QFileDialog.getExistingDirectory(self.w,tr('자동으로 새 사진을 가져올 폴더'))
        if folder and folder not in self.watch_paths:
            self.w.catalog.restore_folder(folder)
            self.watch_paths.append(folder);self.w.catalog.save_preference('watch_folders',self.watch_paths)
            self.timer.start();self.watch_scan()

    def unwatch(self):
        self.watch_paths=[];self.timer.stop();self.w.catalog.save_preference('watch_folders',[])

    def watch_scan(self):
        if self.w.import_busy or self.w.import_scans or self.w.export_running or getattr(self.w,'maintenance_running',False):return
        paths=[p for p in self.watch_paths if Path(p).is_dir()]
        if paths:self.w.import_paths(paths,restore=False)

    def map(self):
        photos=[self.w.catalog.photo(i) for i in self.w.visible_ids]
        dialog=QDialog(self.w);dialog.setWindowTitle(tr('GPS 지도'));dialog.resize(1050,650);box=QVBoxLayout(dialog)
        view=MapView(photos,lambda ident:(self.w.activate(ident),self.w.set_mode(0)),dialog);box.addWidget(view,1)
        box.addWidget(QLabel(tr('GPS 사진 {0}장 · 휠 확대 / 드래그 이동 / 점 더블클릭으로 사진 열기', f'{len(view.points)}')))
        box.addWidget(QLabel(tr('오프라인 지도 · Natural Earth 1:110m · 촬영 위치를 외부로 전송하지 않습니다.')))
        self.dialogs.append(dialog);dialog.show()

    def shutdown(self):
        self.timer.stop()

    def import_lightroom(self):
        w=self.w
        if getattr(w,'maintenance_running',False):return
        if w.import_busy or w.import_scans or w.export_running or w.jobs:
            raise ValueError('진행 중인 사진 처리 작업이 끝난 뒤 가져오기를 시작하세요.')
        path,_=QFileDialog.getOpenFileName(self.w,tr('Lightroom 카탈로그 가져오기'),'','Lightroom (*.lrcat)')
        if not path:return
        from .lightroom_import_dialog import begin_import
        begin_import(w,path)

    def lightroom_report(self):
        from .lightroom_import import report_text
        db=self.w.catalog.db
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='lightroom_import_runs'").fetchone()
        row=db.execute('SELECT summary FROM lightroom_import_runs ORDER BY created DESC LIMIT 1').fetchone() if exists else None
        if not row:
            QMessageBox.information(self.w,tr('Lightroom 가져오기'),tr('저장된 가져오기 결과가 없습니다.'));return
        QMessageBox.information(self.w,tr('최근 Lightroom 가져오기 결과'),report_text(json.loads(row[0])))
