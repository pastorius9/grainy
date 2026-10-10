"""Modal, cancelable migration workflow. All catalog work runs off the UI thread."""
from .i18n import tr
from PySide6.QtWidgets import QMessageBox,QCheckBox
from PySide6.QtCore import Qt
from .maintenance_dialog import MaintenanceDialog
from .lightroom_import import inspect_catalog, import_catalog, report_text


def begin_import(w, path):
    w.commit()
    w.save_keywords()
    w.maintenance_running = True
    timers = [w.photo_model.check_timer, w.extras.timer]
    active = [timer for timer in timers if timer.isActive()]
    for timer in active:
        timer.stop()
    dialog = MaintenanceDialog(w, 'Lightroom 가져오기',
        '사진과 분류를 가져옵니다. 기존 Luma 사진은 별도 사본으로 보존합니다.\n'
        '지원하는 스마트 규칙을 연결하고, 보정값·이력 변환 여부를 다음 화면에서 선택합니다.')
    w.extras.lightroom_dialog = dialog
    dialog.label.setText(tr('Lightroom 카탈로그 확인 중…'))

    def release():
        w.maintenance_running = False
        dialog.finish()
        for timer in active:
            timer.start()

    def failed(error):
        cancelled = dialog.cancel.is_set()
        release()
        if not cancelled:
            w.show_error(error)

    def finished(result):
        release()
        w.extras.last_lightroom_import = result
        if not result['cancelled']:
            w.manager.refresh_collections()
            w.refresh_lists()
        QMessageBox.information(w, tr('Lightroom 가져오기 결과'), report_text(result))

    def reviewed(info):
        if dialog.cancel.is_set():
            release()
            return
        counts = info['counts']
        text = (f'사진과 가상 사본 {counts.get("Adobe_images",0):,}개를 확인했습니다.\n\n'
                '별점·선택 표시·색 라벨·계층 키워드·IPTC·컬렉션과 폴더 스택을 가져옵니다.\n'
                '기존 Luma 보정과 분류를 덮어쓰지 않으며, 변경된 Lightroom 기록은 새 사본으로 가져옵니다.\n\n'
                '지원하는 스마트 컬렉션은 사진의 현재 분류·메타데이터로 검색합니다. '
                '지원하지 않는 조건이 포함되면 전체 규칙을 보관하고 일반 컬렉션으로 가져옵니다.\n\n'
                '아래 선택을 켜면 지원하는 보정값·이력·스냅샷도 변환합니다. '
                'Luma와 Adobe의 현상 방식이 달라 같은 모습은 보장되지 않습니다. '
                '변환하지 못한 항목은 결과에 표시하고 원문을 보관합니다.\n\n'
                '누락된 원본은 참조 경로로 등록됩니다. 가져오기를 진행할까요?')
        review = QMessageBox(dialog)
        review.setWindowTitle(tr('Lightroom 가져오기 범위'))
        review.setTextFormat(Qt.TextFormat.PlainText)
        review.setText(text)
        review.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        review.button(QMessageBox.StandardButton.Yes).setText(tr('가져오기'))
        review.button(QMessageBox.StandardButton.No).setText(tr('취소'))
        review.setDefaultButton(QMessageBox.StandardButton.No)
        conversion = QCheckBox(tr('지원하는 보정값·이력·스냅샷도 변환 (Adobe와 결과가 다를 수 있음)'))
        conversion.setChecked(False)
        review.setCheckBox(conversion)
        answer = review.exec()
        if answer != QMessageBox.StandardButton.Yes or dialog.cancel.is_set():
            release()
            return
        dialog.label.setText(tr('안전 백업을 준비하고 있습니다…'))
        convert_edits = conversion.isChecked()
        w.spawn(lambda: import_catalog(path, w.catalog.directory, dialog.cancel, dialog.updates.put,convert_edits=convert_edits), finished, failed)

    dialog.show()
    w.spawn(lambda: inspect_catalog(path, dialog.cancel), reviewed, failed)
